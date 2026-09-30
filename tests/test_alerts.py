"""Tests for receiving Zabbix alerts through a webhook."""

from typing import Any

from homeassistant.const import CONF_SCAN_INTERVAL, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.zabbix.alerts import (
    DATA_ALERT_SECRET,
    DATA_ALERT_WEBHOOK_ID,
    SECRET_HEADER,
)
from custom_components.zabbix.const import (
    CONF_ALERTS,
    CONF_GROUP_IDS,
    CONF_ITEM_MODE,
    CONF_TAG,
    EVENT_PROBLEM,
)
from custom_components.zabbix.diagnostics import async_get_config_entry_diagnostics

from .fake_zabbix import FakeZabbix, _problem

WEBHOOK_ID = "b" * 64
SECRET = "c" * 64
LAST_ALERT = "sensor.zabbix_last_alert"

ALERT_OPTIONS = {
    CONF_GROUP_IDS: ["2", "7"],
    CONF_ITEM_MODE: "tagged",
    CONF_TAG: "homeassistant",
    CONF_SCAN_INTERVAL: 30,
    CONF_ALERTS: True,
}


async def _setup_with_alerts(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> MockConfigEntry:
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            DATA_ALERT_WEBHOOK_ID: WEBHOOK_ID,
            DATA_ALERT_SECRET: SECRET,
        },
        options=ALERT_OPTIONS,
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


async def _post(
    hass_client_no_auth: ClientSessionGenerator,
    secret: str | None = SECRET,
    payload: Any = None,
) -> int:
    client = await hass_client_no_auth()
    headers = {SECRET_HEADER: secret} if secret is not None else {}
    response = await client.post(
        f"/api/webhook/{WEBHOOK_ID}",
        json=payload
        if payload is not None
        else {"event_id": "905", "event_value": "1", "event_update_status": "0"},
        headers=headers,
    )
    return response.status


async def test_alert_triggers_refresh(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    fake_zabbix: FakeZabbix,
    config_entry: MockConfigEntry,
) -> None:
    await _setup_with_alerts(hass, config_entry)
    assert hass.states.get(LAST_ALERT).state == STATE_UNKNOWN
    events = async_capture_events(hass, EVENT_PROBLEM)
    fake_zabbix.data["problems"].append(_problem("905", "13005", "Disk low", 2))
    polls = len(fake_zabbix.method_calls("problem.get"))

    assert await _post(hass_client_no_auth) == 200
    await hass.async_block_till_done()

    # Refreshed right away, without waiting for the update interval.
    assert len(fake_zabbix.method_calls("problem.get")) == polls + 1
    assert [(event.data["type"], event.data["event_id"]) for event in events] == [
        ("problem", "905")
    ]
    assert hass.states.get(LAST_ALERT).state != STATE_UNKNOWN
    receiver = config_entry.runtime_data.alerts
    assert receiver is not None
    assert receiver.stats.received == 1
    assert receiver.stats.last_event == {
        "event_id": "905",
        "event_value": "1",
        "event_update_status": "0",
    }


@pytest.mark.parametrize("secret", [None, "wrong"])
async def test_alert_needs_secret(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    fake_zabbix: FakeZabbix,
    config_entry: MockConfigEntry,
    secret: str | None,
) -> None:
    await _setup_with_alerts(hass, config_entry)
    polls = len(fake_zabbix.method_calls("problem.get"))
    assert await _post(hass_client_no_auth, secret=secret) == 401
    await hass.async_block_till_done()
    assert len(fake_zabbix.method_calls("problem.get")) == polls
    receiver = config_entry.runtime_data.alerts
    assert receiver is not None
    assert receiver.stats.rejected == 1
    assert receiver.stats.received == 0


async def test_alert_payload_is_optional(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    fake_zabbix: FakeZabbix,
    config_entry: MockConfigEntry,
) -> None:
    await _setup_with_alerts(hass, config_entry)
    client = await hass_client_no_auth()
    response = await client.post(
        f"/api/webhook/{WEBHOOK_ID}", data="not json", headers={SECRET_HEADER: SECRET}
    )
    assert response.status == 200
    assert await _post(hass_client_no_auth, payload=["a list"]) == 200
    receiver = config_entry.runtime_data.alerts
    assert receiver is not None
    assert receiver.stats.received == 2
    assert receiver.stats.last_event == {}


async def test_options_turn_alerts_on_and_off(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "alerts"}
    )
    assert result["step_id"] == "alerts"
    placeholders = result["description_placeholders"]
    assert placeholders["header"] == SECRET_HEADER
    assert "/api/webhook/" in placeholders["url"]
    assert len(placeholders["secret"]) == 64
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ALERTS: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert init_integration.options[CONF_ALERTS] is True
    assert init_integration.data[DATA_ALERT_SECRET] == placeholders["secret"]
    webhook_id = init_integration.data[DATA_ALERT_WEBHOOK_ID]
    assert webhook_id in placeholders["url"]
    assert init_integration.runtime_data.alerts is not None
    assert hass.states.get(LAST_ALERT) is not None

    # The same credentials are shown again, and turning it off removes the sensor.
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "alerts"}
    )
    assert result["description_placeholders"]["secret"] == placeholders["secret"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ALERTS: False}
    )
    await hass.async_block_till_done()
    assert init_integration.runtime_data.alerts is None
    assert entity_registry.async_get(LAST_ALERT) is None
    # Credentials are kept so a Zabbix setup keeps working if turned on again.
    assert init_integration.data[DATA_ALERT_WEBHOOK_ID] == webhook_id


async def test_alerts_off_by_default(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert init_integration.runtime_data.alerts is None
    assert hass.states.get(LAST_ALERT) is None


async def test_alert_diagnostics(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    fake_zabbix: FakeZabbix,
    config_entry: MockConfigEntry,
) -> None:
    await _setup_with_alerts(hass, config_entry)
    assert await _post(hass_client_no_auth) == 200
    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diagnostics["alerts"]["received"] == 1
    assert diagnostics["alerts"]["last_received"] is not None
    assert diagnostics["entry"]["data"][DATA_ALERT_SECRET] == "**REDACTED**"
    assert diagnostics["entry"]["data"][DATA_ALERT_WEBHOOK_ID] == "**REDACTED**"
