"""Tests for the Zabbix actions."""

from datetime import timedelta

from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zabbix.const import DOMAIN

from .fake_zabbix import ApiFailure, FakeZabbix


def _device_id(
    device_registry: dr.DeviceRegistry, entry: MockConfigEntry, suffix: str
) -> str:
    identifier = f"{entry.entry_id}_{suffix}" if suffix else entry.entry_id
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, identifier), entry.entry_id
    )
    assert device is not None
    return device.id


@pytest.mark.parametrize(
    ("service", "action"),
    [
        ("acknowledge_problem", 2),
        ("unacknowledge_problem", 16),
        ("close_problem", 1),
    ],
)
async def test_problem_actions(
    hass: HomeAssistant,
    fake_zabbix: FakeZabbix,
    init_integration: MockConfigEntry,
    service: str,
    action: int,
) -> None:
    await hass.services.async_call(
        DOMAIN,
        service,
        {"config_entry_id": init_integration.entry_id, "event_id": "900"},
        blocking=True,
    )
    assert fake_zabbix.method_calls("event.acknowledge")[-1] == {
        "eventids": ["900"],
        "action": action,
    }


async def test_acknowledge_with_message(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, init_integration: MockConfigEntry
) -> None:
    await hass.services.async_call(
        DOMAIN,
        "acknowledge_problem",
        {
            "config_entry_id": init_integration.entry_id,
            "event_id": ["900", "904"],
            "message": "Looking into it",
        },
        blocking=True,
    )
    assert fake_zabbix.method_calls("event.acknowledge")[-1] == {
        "eventids": ["900", "904"],
        "message": "Looking into it",
        "action": 6,
    }
    await hass.async_block_till_done()
    problems = hass.states.get("sensor.zabbix_problems").attributes["problems"]
    assert all(problem["acknowledged"] for problem in problems)


async def test_invalid_event_id(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    with pytest.raises(Exception, match="event_id"):
        await hass.services.async_call(
            DOMAIN,
            "acknowledge_problem",
            {"config_entry_id": init_integration.entry_id, "event_id": "abc"},
            blocking=True,
        )


async def test_unknown_entry(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "acknowledge_problem",
            {"config_entry_id": "nope", "event_id": "900"},
            blocking=True,
        )
    assert err.value.translation_key == "entry_not_found"


async def test_entry_not_loaded(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    await hass.config_entries.async_unload(init_integration.entry_id)
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "close_problem",
            {"config_entry_id": init_integration.entry_id, "event_id": "900"},
            blocking=True,
        )
    assert err.value.translation_key == "entry_not_loaded"


@pytest.mark.parametrize(
    ("failure", "http_status", "translation_key"),
    [
        (
            ApiFailure(
                -32500,
                "Application error.",
                "No permissions to referred object or it does not exist!",
            ),
            None,
            "permission_denied",
        ),
        (
            ApiFailure(-32500, "Application error.", "Cannot close problem."),
            None,
            "api_error",
        ),
        (None, 503, "cannot_connect"),
    ],
)
async def test_action_errors(
    hass: HomeAssistant,
    fake_zabbix: FakeZabbix,
    init_integration: MockConfigEntry,
    failure: ApiFailure | None,
    http_status: int | None,
    translation_key: str,
) -> None:
    if failure is not None:
        fake_zabbix.failures["event.acknowledge"] = failure
    fake_zabbix.http_status = http_status
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN,
            "close_problem",
            {"config_entry_id": init_integration.entry_id, "event_id": "900"},
            blocking=True,
        )
    assert err.value.translation_key == translation_key
    fake_zabbix.http_status = None


async def test_maintenance(
    hass: HomeAssistant,
    fake_zabbix: FakeZabbix,
    init_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    web = _device_id(device_registry, init_integration, "10500")
    db = _device_id(device_registry, init_integration, "10501")
    await hass.services.async_call(
        DOMAIN,
        "start_maintenance",
        {
            ATTR_DEVICE_ID: [web, db],
            "duration": timedelta(hours=2),
            "collect_data": False,
            "description": "Patching",
        },
        blocking=True,
    )
    params = fake_zabbix.method_calls("maintenance.create")[-1]
    assert params["name"].startswith("Home Assistant: Database 01, Web server 01 (")
    assert params["hosts"] == [{"hostid": "10500"}, {"hostid": "10501"}]
    assert params["maintenance_type"] == 1
    assert params["description"] == "Patching"
    assert params["active_till"] - params["active_since"] == 7200

    # A maintenance created in Zabbix itself is never touched.
    fake_zabbix.data["maintenances"].append(
        {"maintenanceid": "1", "name": "Weekly patching", "hostids": ["10500"]}
    )
    await hass.services.async_call(
        DOMAIN, "end_maintenance", {ATTR_DEVICE_ID: web}, blocking=True
    )
    remaining = {
        m["maintenanceid"]: m["hostids"] for m in fake_zabbix.data["maintenances"]
    }
    assert remaining["1"] == ["10500"]
    assert [hosts for mid, hosts in remaining.items() if mid != "1"] == [["10501"]]
    await hass.services.async_call(
        DOMAIN, "end_maintenance", {ATTR_DEVICE_ID: db}, blocking=True
    )
    assert [m["maintenanceid"] for m in fake_zabbix.data["maintenances"]] == ["1"]
    # Nothing to end.
    await hass.services.async_call(
        DOMAIN, "end_maintenance", {ATTR_DEVICE_ID: db}, blocking=True
    )


async def test_maintenance_name_is_truncated(
    hass: HomeAssistant,
    fake_zabbix: FakeZabbix,
    init_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    fake_zabbix.host("10500")["name"] = "x" * 200
    await init_integration.runtime_data.async_refresh()
    await hass.services.async_call(
        DOMAIN,
        "start_maintenance",
        {
            ATTR_DEVICE_ID: _device_id(device_registry, init_integration, "10500"),
            "duration": {"minutes": 30},
        },
        blocking=True,
    )
    name = fake_zabbix.method_calls("maintenance.create")[-1]["name"]
    assert len(name) == 128
    assert name.endswith(")")


async def test_maintenance_requires_host_devices(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    for device_id in (_device_id(device_registry, init_integration, ""), "missing"):
        with pytest.raises(ServiceValidationError) as err:
            await hass.services.async_call(
                DOMAIN,
                "start_maintenance",
                {ATTR_DEVICE_ID: device_id, "duration": {"hours": 1}},
                blocking=True,
            )
        assert err.value.translation_key == "not_a_host"
