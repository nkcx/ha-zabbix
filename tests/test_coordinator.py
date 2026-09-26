"""Tests for the Zabbix coordinator: polling, dynamic entities and events."""

from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import CONF_SCAN_INTERVAL, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.zabbix.const import (
    CONF_GROUP_IDS,
    CONF_ITEM_MODE,
    CONF_TAG,
    DOMAIN,
    EVENT_PROBLEM,
)
from custom_components.zabbix.coordinator import ZabbixCoordinator, key_matches

from .fake_zabbix import ApiFailure, FakeZabbix, _host, _interface, _item, _problem

WEB_CPU = "sensor.web_server_01_linux_cpu_utilization"


def _coordinator(entry: MockConfigEntry) -> ZabbixCoordinator:
    return entry.runtime_data


async def _refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await _coordinator(entry).async_refresh()
    await hass.async_block_till_done()


async def _refresh_metadata(
    hass: HomeAssistant, entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.tick(timedelta(minutes=11))
    await _refresh(hass, entry)


def test_key_matches() -> None:
    assert key_matches("zabbix[wcache,values]", ("zabbix[wcache,values]",))
    assert not key_matches("zabbix[wcache,values,float]", ("zabbix[wcache,values]",))
    assert key_matches("zabbix[rcache,buffer,pused]", ("zabbix[*,pused]",))
    assert not key_matches("zabbix[x]", ("zabbix[?]",))


async def test_problem_counts(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    problems = hass.states.get("sensor.zabbix_problems")
    assert problems is not None
    # Suppressed and symptom problems are hidden, like Zabbix's default views;
    # problems on hosts outside the selected groups still count.
    assert problems.state == "3"
    assert problems.attributes["suppressed"] == 1
    assert problems.attributes["symptoms"] == 1
    assert problems.attributes["high"] == 1
    assert problems.attributes["average"] == 1
    assert problems.attributes["information"] == 1
    assert [p["event_id"] for p in problems.attributes["problems"]] == [
        "900",
        "901",
        "904",
    ]
    assert problems.attributes["problems"][0]["hosts"] == ["Web server 01"]
    assert hass.states.get("sensor.database_01_highest_problem_severity").state == (
        "average"
    )
    db_problem = hass.states.get("binary_sensor.database_01_problem")
    assert db_problem.state == "on"
    assert db_problem.attributes["count"] == 1
    assert hass.states.get("binary_sensor.switch_01_problem").state == "off"
    assert hass.states.get("sensor.switch_01_highest_problem_severity").state == "none"


async def test_update_failure_and_recovery(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, init_integration: MockConfigEntry
) -> None:
    fake_zabbix.http_status = 503
    await _refresh(hass, init_integration)
    assert hass.states.get(WEB_CPU).state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.zabbix_problems").state == STATE_UNAVAILABLE
    fake_zabbix.http_status = None
    await _refresh(hass, init_integration)
    assert hass.states.get(WEB_CPU).state == "3.2"


async def test_auth_failure_during_update(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, init_integration: MockConfigEntry
) -> None:
    fake_zabbix.failures["problem.get"] = ApiFailure(
        -32602, "Invalid params.", "Not authorized."
    )
    await _refresh(hass, init_integration)
    assert any(
        flow["context"]["source"] == SOURCE_REAUTH
        for flow in hass.config_entries.flow.async_progress()
    )


async def test_new_host_is_added(
    hass: HomeAssistant,
    fake_zabbix: FakeZabbix,
    init_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    fake_zabbix.data["hosts"].append(
        _host(
            "10800", "new-01", "New 01", ["2"], [_interface("30", 1, "192.0.2.30", 1)]
        )
    )
    fake_zabbix.data["items"].append(
        _item(
            "28000",
            "10800",
            "system.cpu.util",
            "Linux: CPU utilization",
            units="%",
            value="1",
            tags=("homeassistant",),
            interfaceid="30",
        )
    )
    await _refresh(hass, init_integration)
    assert device_registry.async_get_device_by_identifier(
        (DOMAIN, f"{init_integration.entry_id}_10800"), init_integration.entry_id
    )
    assert hass.states.get("sensor.new_01_linux_cpu_utilization").state == "1.0"
    assert hass.states.get("sensor.new_01_agent_availability").state == "available"
    assert hass.states.get("binary_sensor.new_01_problem").state == "off"


async def test_removed_host_is_cleaned_up(
    hass: HomeAssistant,
    fake_zabbix: FakeZabbix,
    init_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    entry_id = init_integration.entry_id
    identifier = (DOMAIN, f"{entry_id}_10600")
    assert device_registry.async_get_device_by_identifier(identifier, entry_id)
    fake_zabbix.data["hosts"] = [
        host for host in fake_zabbix.data["hosts"] if host["hostid"] != "10600"
    ]
    await _refresh(hass, init_integration)
    assert device_registry.async_get_device_by_identifier(identifier, entry_id) is None
    assert entity_registry.async_get("binary_sensor.switch_01_problem") is None


async def test_removed_item_is_cleaned_up(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    fake_zabbix: FakeZabbix,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    entity_id = "sensor.web_server_01_cpu_temperature"
    assert entity_registry.async_get(entity_id)
    fake_zabbix.data["items"] = [
        item for item in fake_zabbix.data["items"] if item["itemid"] != "24013"
    ]
    # Item metadata is only re-read on the slow cycle.
    await _refresh(hass, init_integration)
    assert entity_registry.async_get(entity_id)
    await _refresh_metadata(hass, init_integration, freezer)
    assert entity_registry.async_get(entity_id) is None
    assert hass.states.get(entity_id) is None


async def test_recreated_item_keeps_its_entity(
    hass: HomeAssistant,
    fake_zabbix: FakeZabbix,
    init_integration: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    item = fake_zabbix.item("24000")
    item["itemid"] = "24900"
    item["lastvalue"] = "9.9"
    await _refresh_metadata(hass, init_integration, freezer)
    assert hass.states.get(WEB_CPU).state == "9.9"


async def test_metadata_changes_are_followed(
    hass: HomeAssistant,
    fake_zabbix: FakeZabbix,
    init_integration: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    entity_id = "sensor.web_server_01_custom_metric"
    assert "device_class" not in hass.states.get(entity_id).attributes
    fake_zabbix.item("24014")["units"] = "B"
    await _refresh_metadata(hass, init_integration, freezer)
    state = hass.states.get(entity_id)
    assert state.attributes["device_class"] == "data_size"
    assert state.attributes["unit_of_measurement"] == "B"


async def test_new_version_updates_device(
    hass: HomeAssistant,
    fake_zabbix: FakeZabbix,
    init_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    fake_zabbix.version = "7.4.15"
    await _refresh_metadata(hass, init_integration, freezer)
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, init_integration.entry_id), init_integration.entry_id
    )
    assert device.sw_version == "7.4.15"
    assert hass.states.get("sensor.zabbix_version").state == "7.4.15"


async def test_missing_groups_issue(
    hass: HomeAssistant,
    fake_zabbix: FakeZabbix,
    init_integration: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    issue_id = f"missing_groups_{init_integration.entry_id}"
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None
    groups = fake_zabbix.data["groups"]
    fake_zabbix.data["groups"] = [group for group in groups if group["groupid"] != "7"]
    await _refresh_metadata(hass, init_integration, freezer)
    issue = issue_registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_placeholders == {
        "title": init_integration.title,
        "count": "1",
    }
    fake_zabbix.data["groups"] = groups
    await _refresh_metadata(hass, init_integration, freezer)
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None


async def test_server_items(
    hass: HomeAssistant,
    fake_zabbix: FakeZabbix,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    assert hass.states.get("sensor.zabbix_zabbix_server_queue").state == "0"
    assert (
        hass.states.get(
            "sensor.zabbix_zabbix_server_number_of_processed_values_per_second"
        ).state
        == "12.5"
    )
    # Per-process utilization is created disabled and is not polled.
    poller = entity_registry.async_get(
        "sensor.zabbix_zabbix_server_utilization_of_poller_data_collector_processes_in"
    )
    assert poller is not None
    assert poller.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    polled = fake_zabbix.method_calls("item.get")[-1]["itemids"]
    assert "23002" not in polled
    assert "23000" in polled
    # zabbix[version] is represented by the version sensor instead.
    assert not any(
        entry.unique_id.endswith("_zabbix[version]")
        for entry in er.async_entries_for_config_entry(
            entity_registry, init_integration.entry_id
        )
    )
    # Per-host internal items are not server items.
    assert (
        hass.states.get("sensor.web_server_01_linux_zabbix_agent_availability") is None
    )
    assert hass.states.get("sensor.zabbix_hosts").state == "6"


async def test_enabled_entity_is_polled(
    hass: HomeAssistant,
    fake_zabbix: FakeZabbix,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    entity_id = (
        "sensor.zabbix_zabbix_server_utilization_of_poller_data_collector_processes_in"
    )
    entity_registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "1.5"


async def test_multiple_server_hosts_are_named(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, config_entry: MockConfigEntry
) -> None:
    fake_zabbix.data["hosts"].append(
        _host("10085", "Zabbix node 2", "Zabbix node 2", ["4"], [])
    )
    fake_zabbix.data["items"].append(
        _item(
            "23100",
            "10085",
            "zabbix[queue]",
            "Zabbix server: Queue",
            type_=5,
            value_type=3,
            value="4",
        )
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert (
        hass.states.get("sensor.zabbix_zabbix_server_zabbix_server_queue").state == "0"
    )
    assert (
        hass.states.get("sensor.zabbix_zabbix_node_2_zabbix_server_queue").state == "4"
    )


@pytest.mark.parametrize(
    "entry_options",
    [
        {
            CONF_GROUP_IDS: ["2", "4"],
            CONF_ITEM_MODE: "tagged",
            CONF_TAG: "homeassistant",
            CONF_SCAN_INTERVAL: 30,
        }
    ],
)
async def test_tagged_mode(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert hass.states.get("sensor.zabbix_zabbix_server_queue") is None
    assert hass.states.get("sensor.zabbix_hosts") is None
    assert hass.states.get("sensor.zabbix_problems").state == "3"
    assert hass.states.get("sensor.zabbix_version").state == "7.4.14"
    assert hass.states.get(WEB_CPU).state == "3.2"
    # The Zabbix server's machine is an ordinary host device.
    assert hass.states.get("sensor.zabbix_server_agent_availability").state == (
        "available"
    )


@pytest.mark.parametrize(
    "entry_options",
    [
        {
            CONF_GROUP_IDS: ["2", "4"],
            CONF_ITEM_MODE: "all",
            CONF_TAG: "homeassistant",
            CONF_SCAN_INTERVAL: 30,
        }
    ],
)
async def test_all_mode(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    assert hass.states.get("sensor.web_server_01_linux_memory_utilization").state == (
        "41.0"
    )
    assert (
        hass.states.get(
            "sensor.zabbix_zabbix_server_utilization_of_poller_data_collector_processes_in"
        ).state
        == "1.5"
    )
    # The server's machine gets its non-internal items on its own device...
    assert hass.states.get("sensor.zabbix_server_linux_cpu_utilization").state == "5.0"
    # ...and its per-host internal items too.
    assert (
        hass.states.get("sensor.zabbix_server_linux_zabbix_agent_availability").state
        == "1"
    )
    assert hass.states.get("sensor.web_server_01_screenshot") is None


async def _set_mode(hass: HomeAssistant, entry: MockConfigEntry, mode: str) -> None:
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_ITEM_MODE: mode}
    )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()


async def test_item_mode_switch_round_trip(
    hass: HomeAssistant,
    fake_zabbix: FakeZabbix,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    poller = (
        "sensor.zabbix_zabbix_server_utilization_of_poller_data_collector_processes_in"
    )
    stats = "sensor.zabbix_zabbix_server_zabbix_stats"
    assert entity_registry.async_get(poller).disabled
    assert entity_registry.async_get(stats).disabled

    # All items: integration-disabled entities are enabled and polled right away.
    await _set_mode(hass, init_integration, "all")
    assert not entity_registry.async_get(poller).disabled
    assert "23002" in fake_zabbix.method_calls("item.get")[-1]["itemids"]
    assert hass.states.get(poller).state == "1.5"

    # The user disables one of them while in All items mode.
    entity_registry.async_update_entity(
        stats, disabled_by=er.RegistryEntryDisabler.USER
    )
    await hass.async_block_till_done()

    # Back to the default mode: the integration's enabling is undone...
    await _set_mode(hass, init_integration, "server_and_tagged")
    assert (
        entity_registry.async_get(poller).disabled_by
        is er.RegistryEntryDisabler.INTEGRATION
    )
    assert "23002" not in fake_zabbix.method_calls("item.get")[-1]["itemids"]
    # ...the user's choice is kept, and nothing is remembered any more.
    assert entity_registry.async_get(stats).disabled_by is er.RegistryEntryDisabler.USER
    assert init_integration.data["mode_enabled"] == []


async def test_user_enabled_entity_stays_enabled(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    poller = (
        "sensor.zabbix_zabbix_server_utilization_of_poller_data_collector_processes_in"
    )
    entity_registry.async_update_entity(poller, disabled_by=None)
    await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()
    await _set_mode(hass, init_integration, "all")
    await _set_mode(hass, init_integration, "server_and_tagged")
    assert not entity_registry.async_get(poller).disabled


async def test_problems_hidden_by_the_frontend(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, init_integration: MockConfigEntry
) -> None:
    # Trigger disabled, host unmonitored, or dependent on a trigger in problem
    # state: Zabbix's trigger.get(monitored, skipDependent) no longer returns it.
    fake_zabbix.data["hidden_triggers"] = ["13000"]
    await _refresh(hass, init_integration)
    problems = hass.states.get("sensor.zabbix_problems")
    assert problems.state == "2"
    assert problems.attributes["hidden"] == 1
    assert problems.attributes["high"] == 0
    assert hass.states.get("binary_sensor.web_server_01_problem").state == "off"
    assert hass.states.get("sensor.web_server_01_highest_problem_severity").state == (
        "none"
    )
    params = fake_zabbix.method_calls("trigger.get")[-1]
    assert params["monitored"] is True
    assert params["skipDependent"] is True
    fake_zabbix.data["hidden_triggers"] = []
    await _refresh(hass, init_integration)
    assert hass.states.get("sensor.zabbix_problems").state == "3"


async def test_boot_time_ignores_jitter(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, init_integration: MockConfigEntry
) -> None:
    entity_id = "sensor.web_server_01_linux_system_uptime"
    boot = hass.states.get(entity_id).state
    item = fake_zabbix.item("24002")
    item["lastvalue"] = str(86400 + 29)
    item["lastclock"] = str(int(item["lastclock"]) + 30)
    await _refresh(hass, init_integration)
    assert hass.states.get(entity_id).state == boot
    item["lastvalue"] = "10"
    await _refresh(hass, init_integration)
    assert hass.states.get(entity_id).state != boot


async def test_item_states(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert hass.states.get("sensor.web_server_01_linux_zabbix_agent_ping").state == "Up"
    ping = hass.states.get("sensor.web_server_01_icmp_icmp_ping")
    assert ping.state == "unknown"
    assert ping.attributes["raw_value"] == "3"
    motd = hass.states.get("sensor.web_server_01_message_of_the_day")
    assert len(motd.state) == 255
    assert len(motd.attributes["raw_value"]) == 300
    assert hass.states.get("sensor.web_server_01_fs_space_used_in").state == (
        STATE_UNAVAILABLE
    )
    assert hass.states.get("sensor.database_01_linux_cpu_utilization").state == (
        "unknown"
    )


async def test_problem_events(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, init_integration: MockConfigEntry
) -> None:
    events = async_capture_events(hass, EVENT_PROBLEM)
    problems: list[dict[str, Any]] = fake_zabbix.data["problems"]
    by_id = {problem["eventid"]: problem for problem in problems}
    by_id["901"]["acknowledged"] = "0"
    by_id["901"]["severity"] = "4"
    by_id["904"]["acknowledged"] = "1"
    by_id["902"]["suppressed"] = "0"
    by_id["903"]["suppressed"] = "1"
    fake_zabbix.data["problems"] = [
        problem for problem in problems if problem["eventid"] != "900"
    ] + [_problem("905", "13005", "Disk space is low", 2)]
    await _refresh(hass, init_integration)
    changes = sorted((event.data["type"], event.data["event_id"]) for event in events)
    assert changes == [
        ("acknowledged", "904"),
        ("problem", "905"),
        ("resolved", "900"),
        ("severity_changed", "901"),
        ("suppressed", "903"),
        ("unacknowledged", "901"),
        ("unsuppressed", "902"),
    ]
    new = next(event for event in events if event.data["event_id"] == "905")
    assert new.data["hosts"] == ["Web server 01"]
    assert new.data["severity"] == "warning"
    assert new.data["config_entry_id"] == init_integration.entry_id
    web_events = hass.states.get("event.web_server_01_problem_events")
    assert web_events.attributes["event_type"] in ("problem", "resolved")
    assert web_events.attributes["name"] in (
        "Disk space is low",
        "High CPU utilization",
    )
    server_events = hass.states.get("event.zabbix_problem_events")
    assert server_events.state != "unknown"
    switch_events = hass.states.get("event.switch_01_problem_events")
    assert switch_events.state == "unknown"


async def test_no_events_on_first_refresh(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, config_entry: MockConfigEntry
) -> None:
    events = async_capture_events(hass, EVENT_PROBLEM)
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert events == []


async def test_event_entities_follow_availability(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, init_integration: MockConfigEntry
) -> None:
    fake_zabbix.http_status = 503
    await _refresh(hass, init_integration)
    assert hass.states.get("event.zabbix_problem_events").state == STATE_UNAVAILABLE
    fake_zabbix.http_status = None
    await _refresh(hass, init_integration)
    assert hass.states.get("event.zabbix_problem_events").state == "unknown"
