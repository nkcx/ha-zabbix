"""Tests for publishing Home Assistant states to Zabbix."""

import asyncio
from collections.abc import Generator
from dataclasses import dataclass
import json
import logging
from typing import Any, ClassVar
from unittest.mock import patch

from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from zabbix_utils import ItemValue
from zabbix_utils.exceptions import ProcessingError

from custom_components.zabbix.const import (
    CONF_GROUP_IDS,
    CONF_ITEM_MODE,
    CONF_PUBLISH_HOST,
    CONF_PUBLISH_PORT,
    CONF_PUBLISH_SERVER,
    CONF_PUBLISH_STRINGS,
    CONF_TAG,
    DOMAIN,
)
from custom_components.zabbix.publisher import state_values, value_key

from .fake_zabbix import FakeZabbix, _host, _item


@dataclass
class _Response:
    processed: int
    failed: int


class FakeSender:
    """Records what would be sent over the trapper protocol."""

    instances: ClassVar[list[FakeSender]] = []

    def __init__(self, server: str, port: int) -> None:
        """Initialize."""
        self.server = server
        self.port = port
        self.batches: list[list[ItemValue]] = []
        self.errors: list[Exception] = []
        FakeSender.instances.append(self)

    async def send(self, items: list[ItemValue]) -> _Response:
        """Record items, or raise the next queued error."""
        if self.errors:
            raise self.errors.pop(0)
        self.batches.append(list(items))
        return _Response(processed=len(items), failed=0)

    def sent(self) -> dict[str, str]:
        """Return the last value sent per key."""
        return {item.key: item.value for batch in self.batches for item in batch}

    def discovery(self, item_type: str = "float") -> list[str]:
        """Return the keys in the last discovery sent for a type."""
        values = [
            item.value
            for batch in self.batches
            for item in batch
            if item.key == f"homeassistant.{item_type}s_discovery"
        ]
        return [entry["{#KEY}"] for entry in json.loads(values[-1])] if values else []


@pytest.fixture(autouse=True)
def fast_publisher() -> Generator[None]:
    """Use a fake sender and short timeouts."""
    FakeSender.instances = []
    with (
        patch("custom_components.zabbix.AsyncSender", FakeSender),
        patch("custom_components.zabbix.publisher.BATCH_TIMEOUT", 0.01),
        patch("custom_components.zabbix.publisher.RETRY_DELAY", 0),
    ):
        yield


PUBLISH_OPTIONS = {
    CONF_GROUP_IDS: ["2"],
    CONF_ITEM_MODE: "tagged",
    CONF_TAG: "homeassistant",
    CONF_SCAN_INTERVAL: 30,
    CONF_PUBLISH_HOST: "Home Assistant",
    CONF_PUBLISH_SERVER: "",
    CONF_PUBLISH_PORT: 10051,
    CONF_PUBLISH_STRINGS: False,
    "exclude_entities": ["sensor.secret"],
}


async def _flush(hass: HomeAssistant) -> None:
    """Let the publisher's background worker collect and send a batch."""
    for _ in range(5):
        await hass.async_block_till_done()
        await asyncio.sleep(0.03)


def _sender() -> FakeSender:
    assert len(FakeSender.instances) == 1
    return FakeSender.instances[0]


@pytest.mark.parametrize(
    ("state", "attributes", "strings", "expected"),
    [
        ("21.5", {}, False, ({"sensor.x": 21.5}, {})),
        ("on", {}, False, ({"sensor.x": 1.0}, {})),
        ("off", {}, False, ({"sensor.x": 0.0}, {})),
        ("home", {}, False, ({"sensor.x": 1.0}, {})),
        ("heat", {}, False, ({}, {})),
        ("heat", {}, True, ({}, {"sensor.x": "heat"})),
        (
            "12",
            {"battery": 80, "flag": True, "name": "x", "bad": float("inf"), "n": None},
            False,
            ({"sensor.x": 12.0, "sensor.x/battery": 80.0, "sensor.x/flag": 1.0}, {}),
        ),
    ],
)
def test_state_values(
    state: str,
    attributes: dict[str, Any],
    strings: bool,
    expected: tuple[dict[str, float], dict[str, str]],
) -> None:
    values = state_values(State("sensor.x", state, attributes), strings)
    assert values is not None
    assert (values.floats, values.strings) == expected


@pytest.mark.parametrize("state", ["unknown", "unavailable", ""])
def test_state_values_skipped(state: str) -> None:
    assert state_values(State("sensor.x", state), True) is None


def test_value_key() -> None:
    assert value_key("float", "sensor.x") == "homeassistant.float[sensor.x]"
    assert value_key("float", "sensor.x/battery") == (
        "homeassistant.float[sensor.x/battery]"
    )
    assert value_key("string", 'a,"b]') == 'homeassistant.string["a,\\"b]"]'


@pytest.mark.parametrize("entry_options", [PUBLISH_OPTIONS])
async def test_publishes_states(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_zabbix: FakeZabbix,
) -> None:
    hass.states.async_set("sensor.temperature", "21.5", {"battery": 90})
    hass.states.async_set("sensor.secret", "1")
    hass.states.async_set("switch.fan", "on")
    hass.states.async_set("climate.hall", "heat")
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await _flush(hass)
    sender = _sender()
    assert sender.server == "127.0.0.1"
    assert sender.port == 10051
    # Current states are sent at startup, with a full discovery list.
    assert sender.discovery() == [
        "sensor.temperature",
        "sensor.temperature/battery",
        "switch.fan",
    ]
    assert sender.discovery("string") == []
    sent = sender.sent()
    assert sent["homeassistant.float[sensor.temperature]"] == "21.5"
    assert sent["homeassistant.float[switch.fan]"] == "1.0"
    assert "homeassistant.float[sensor.secret]" not in sent
    # The integration's own entities are never sent back to Zabbix.
    assert not any("zabbix_problems" in key for key in sent)
    assert all(item.host == "Home Assistant" for b in sender.batches for item in b)
    first = sender.batches[0][2]
    assert first.clock is not None

    # Changes are sent; a new entity triggers a new, complete discovery list.
    sender.batches.clear()
    hass.states.async_set("sensor.temperature", "22")
    hass.states.async_set("sensor.humidity", "40")
    await _flush(hass)
    assert sender.sent()["homeassistant.float[sensor.temperature]"] == "22.0"
    assert sender.discovery() == [
        "sensor.humidity",
        "sensor.temperature",
        "sensor.temperature/battery",
        "switch.fan",
    ]

    # No new keys: no discovery.
    sender.batches.clear()
    hass.states.async_set("sensor.humidity", "41")
    await _flush(hass)
    assert sender.discovery() == []

    # The hourly rediscovery sends the complete list again.
    publisher = config_entry.runtime_data.publisher
    assert publisher is not None
    publisher._async_request_rediscovery(None)  # type: ignore[arg-type]
    await _flush(hass)
    assert len(sender.discovery()) == 4

    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    "entry_options",
    [
        {
            **PUBLISH_OPTIONS,
            CONF_PUBLISH_STRINGS: True,
            CONF_PUBLISH_SERVER: "proxy.example.com",
            CONF_PUBLISH_PORT: 10052,
            "include_domains": ["climate"],
        }
    ],
)
async def test_publishes_strings_with_filter(
    hass: HomeAssistant, config_entry: MockConfigEntry, fake_zabbix: FakeZabbix
) -> None:
    hass.states.async_set("climate.hall", "heat", {"temperature": 20})
    hass.states.async_set("sensor.temperature", "21.5")
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await _flush(hass)
    sender = _sender()
    assert (sender.server, sender.port) == ("proxy.example.com", 10052)
    assert sender.discovery("string") == ["climate.hall"]
    assert sender.discovery() == ["climate.hall/temperature"]
    assert sender.sent()["homeassistant.string[climate.hall]"] == "heat"
    assert "homeassistant.float[sensor.temperature]" not in sender.sent()
    await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.mark.parametrize("entry_options", [PUBLISH_OPTIONS])
async def test_send_errors(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_zabbix: FakeZabbix,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await _flush(hass)
    sender = _sender()
    publisher = config_entry.runtime_data.publisher
    assert publisher is not None

    # Retries succeed: nothing is lost.
    sender.errors = [ProcessingError("refused")]
    hass.states.async_set("sensor.a", "1")
    await _flush(hass)
    assert sender.sent()["homeassistant.float[sensor.a]"] == "1.0"
    assert publisher.stats.lost == 0

    # All tries fail: values are lost and the error is logged once.
    caplog.set_level(logging.INFO)
    sender.errors = [OSError("down")] * 8
    hass.states.async_set("sensor.a", "2")
    await _flush(hass)
    hass.states.async_set("sensor.a", "3")
    await _flush(hass)
    assert publisher.stats.lost > 0
    assert caplog.text.count("Error sending to Zabbix host") == 1
    assert publisher.stats.last_error == "down"

    hass.states.async_set("sensor.a", "4")
    await _flush(hass)
    assert "Sending to Zabbix resumed" in caplog.text
    assert publisher.diagnostics()["sent"] > 0
    await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.mark.parametrize("entry_options", [PUBLISH_OPTIONS])
async def test_old_events_are_dropped(
    hass: HomeAssistant, config_entry: MockConfigEntry, fake_zabbix: FakeZabbix
) -> None:
    config_entry.add_to_hass(hass)
    with patch("custom_components.zabbix.publisher.MAX_EVENT_AGE", -1):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        hass.states.async_set("sensor.a", "1")
        await _flush(hass)
    publisher = config_entry.runtime_data.publisher
    assert publisher is not None
    assert publisher.stats.dropped > 0
    assert "homeassistant.float[sensor.a]" not in _sender().sent()
    await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.mark.parametrize("entry_options", [PUBLISH_OPTIONS])
async def test_publish_host_checks(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_zabbix: FakeZabbix,
    issue_registry: ir.IssueRegistry,
) -> None:
    entry_id = config_entry.entry_id
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert issue_registry.async_get_issue(DOMAIN, f"publish_host_missing_{entry_id}")
    await hass.config_entries.async_unload(entry_id)

    fake_zabbix.data["hosts"].append(
        _host("10900", "Home Assistant", "Home Assistant", ["9"], [])
    )
    assert await hass.config_entries.async_setup(entry_id)
    await hass.async_block_till_done()
    assert not issue_registry.async_get_issue(
        DOMAIN, f"publish_host_missing_{entry_id}"
    )
    assert issue_registry.async_get_issue(
        DOMAIN, f"publish_template_missing_{entry_id}"
    )
    await hass.config_entries.async_unload(entry_id)

    # A discovery rule, not an item: item.get doesn't return discovery rules.
    fake_zabbix.data["items"].append(
        _item("29001", "10900", "homeassistant.floats_discovery", "Not a rule")
    )
    assert await hass.config_entries.async_setup(entry_id)
    await hass.async_block_till_done()
    assert issue_registry.async_get_issue(
        DOMAIN, f"publish_template_missing_{entry_id}"
    )
    await hass.config_entries.async_unload(entry_id)
    fake_zabbix.data["discovery_rules"] = [
        {"itemid": "29000", "hostid": "10900", "key_": "homeassistant.floats_discovery"}
    ]
    assert await hass.config_entries.async_setup(entry_id)
    await hass.async_block_till_done()
    assert not issue_registry.async_get_issue(
        DOMAIN, f"publish_template_missing_{entry_id}"
    )
    await hass.config_entries.async_unload(entry_id)


async def test_no_publisher_by_default(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert init_integration.runtime_data.publisher is None
    assert FakeSender.instances == []
