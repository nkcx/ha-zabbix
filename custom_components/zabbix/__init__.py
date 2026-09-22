"""The Zabbix integration."""

from homeassistant.const import CONF_URL, CONF_VERIFY_SSL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import ZabbixClient
from .const import CONF_API_TOKEN, DOMAIN
from .coordinator import ZabbixConfigEntry, ZabbixCoordinator
from .services import async_setup_services
from .unique_ids import host_device_identifier

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.EVENT, Platform.SENSOR]

# The core integration used YAML; this one is set up from the UI only. A leftover
# `zabbix:` YAML block raises a repair issue asking the user to remove it.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Zabbix actions."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ZabbixConfigEntry) -> bool:
    """Set up Zabbix from a config entry."""
    client = ZabbixClient(
        entry.data[CONF_URL],
        entry.data[CONF_API_TOKEN],
        async_get_clientsession(hass, verify_ssl=entry.data[CONF_VERIFY_SSL]),
    )
    coordinator = ZabbixCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ZabbixConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ZabbixConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow removing a host device only once Zabbix no longer reports the host."""
    data = entry.runtime_data.data
    return (
        not any(
            host_device_identifier(entry.entry_id, host_id) in device.identifiers
            for host_id in data.hosts
        )
        and (DOMAIN, entry.entry_id) not in device.identifiers
    )
