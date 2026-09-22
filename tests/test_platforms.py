"""Snapshot tests of every entity the integration creates."""

from unittest.mock import PropertyMock, patch

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    snapshot_platform,
)
from syrupy.assertion import SnapshotAssertion


@pytest.mark.parametrize(
    "platform", [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.EVENT]
)
async def test_entities(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    platform: Platform,
) -> None:
    config_entry.add_to_hass(hass)
    with (
        patch("custom_components.zabbix.PLATFORMS", [platform]),
        patch(
            "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
            new_callable=PropertyMock,
            return_value=True,
        ),
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    await snapshot_platform(hass, entity_registry, snapshot, config_entry.entry_id)
