"""Tests for Zabbix diagnostics."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
    get_diagnostics_for_device,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator
from syrupy.assertion import SnapshotAssertion
from syrupy.filters import props

from custom_components.zabbix.const import DOMAIN


async def test_entry_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
    snapshot: SnapshotAssertion,
) -> None:
    diagnostics = await get_diagnostics_for_config_entry(
        hass, hass_client, init_integration
    )
    assert diagnostics["entry"]["data"]["api_token"] == "**REDACTED**"
    assert diagnostics == snapshot(exclude=props("url"))


async def test_device_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    entry_id = init_integration.entry_id
    for identifier in (f"{entry_id}_10501", entry_id):
        device = device_registry.async_get_device_by_identifier(
            (DOMAIN, identifier), entry_id
        )
        assert device is not None
        assert (
            await get_diagnostics_for_device(
                hass, hass_client, init_integration, device
            )
            == snapshot
        )
