"""Tests for setting up and unloading the Zabbix integration."""

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zabbix import async_remove_config_entry_device
from custom_components.zabbix.const import DOMAIN

from .fake_zabbix import ApiFailure, FakeZabbix


async def test_setup_and_unload(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert init_integration.state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert init_integration.state is ConfigEntryState.NOT_LOADED


async def test_setup_auth_failure_starts_reauth(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, config_entry: MockConfigEntry
) -> None:
    fake_zabbix.token = "revoked"
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


async def test_setup_auth_failure_during_first_update(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, config_entry: MockConfigEntry
) -> None:
    fake_zabbix.failures["host.get"] = ApiFailure(
        -32500, "Application error.", "API token expired."
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert any(
        flow["context"]["source"] == SOURCE_REAUTH
        for flow in hass.config_entries.flow.async_progress()
    )


async def test_setup_retry_when_unreachable(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, config_entry: MockConfigEntry
) -> None:
    fake_zabbix.http_status = 503
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_unsupported_version(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, config_entry: MockConfigEntry
) -> None:
    fake_zabbix.version = "6.4.0"
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert config_entry.reason is not None
    assert "6.4.0" in config_entry.reason


async def test_yaml_configuration_raises_issue(
    hass: HomeAssistant, issue_registry: ir.IssueRegistry
) -> None:
    assert await async_setup_component(
        hass, DOMAIN, {DOMAIN: {"host": "zabbix.example.com"}}
    )
    assert issue_registry.async_get_issue(
        "homeassistant", f"config_entry_only_{DOMAIN}"
    )


async def test_remove_config_entry_device(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    entry_id = init_integration.entry_id
    host_device = device_registry.async_get_device_by_identifier(
        (DOMAIN, f"{entry_id}_10500"), entry_id
    )
    service_device = device_registry.async_get_device_by_identifier(
        (DOMAIN, entry_id), entry_id
    )
    assert host_device is not None
    assert service_device is not None
    assert not await async_remove_config_entry_device(
        hass, init_integration, host_device
    )
    assert not await async_remove_config_entry_device(
        hass, init_integration, service_device
    )
    stale = device_registry.async_get_or_create(
        config_entry_id=entry_id, identifiers={(DOMAIN, f"{entry_id}_424242")}
    )
    assert await async_remove_config_entry_device(hass, init_integration, stale)
