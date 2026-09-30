"""The Zabbix integration."""

from typing import Any

from homeassistant.const import CONF_URL, CONF_VERIFY_SSL, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entityfilter import convert_filter
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol
from yarl import URL
from zabbix_utils import AsyncSender

from .alerts import DATA_ALERT_SECRET, DATA_ALERT_WEBHOOK_ID, AlertReceiver
from .api import ZabbixClient
from .config_flow import PUBLISH_FILTER_KEYS
from .const import (
    CONF_ALERTS,
    CONF_API_TOKEN,
    CONF_PUBLISH_HOST,
    CONF_PUBLISH_PORT,
    CONF_PUBLISH_SERVER,
    CONF_PUBLISH_STRINGS,
    DEFAULT_PUBLISH_PORT,
    DOMAIN,
)
from .coordinator import ZabbixConfigEntry, ZabbixCoordinator
from .publisher import ZabbixPublisher
from .services import async_setup_services
from .unique_ids import host_device_identifier

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.EVENT, Platform.SENSOR]


# Accept the built-in integration's YAML so a leftover `zabbix:` block doesn't
# break startup; it only raises a repair issue explaining how to migrate.
CONFIG_SCHEMA = vol.Schema({vol.Optional(DOMAIN): dict}, extra=vol.ALLOW_EXTRA)

ISSUE_YAML = "yaml_configuration"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Zabbix actions and flag leftover YAML configuration."""
    async_setup_services(hass)
    if (yaml_config := config.get(DOMAIN)) is not None:
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_YAML,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_YAML,
            translation_placeholders={
                "publish_host": str(yaml_config.get("publish_states_host", "-")),
            },
        )
    return True


def create_publisher(
    hass: HomeAssistant, entry: ZabbixConfigEntry
) -> ZabbixPublisher | None:
    """Return the publisher configured in the options, if any."""
    options: dict[str, Any] = dict(entry.options)
    if not (host := options.get(CONF_PUBLISH_HOST)):
        return None
    server = options.get(CONF_PUBLISH_SERVER) or URL(entry.data[CONF_URL]).host
    sender = AsyncSender(
        server=server,
        port=int(options.get(CONF_PUBLISH_PORT, DEFAULT_PUBLISH_PORT)),
    )
    entity_filter = convert_filter(
        {key: list(options.get(key, [])) for key in PUBLISH_FILTER_KEYS}
    )
    return ZabbixPublisher(
        hass,
        entry.entry_id,
        sender,
        host=host,
        entity_filter=entity_filter,
        publish_strings=bool(options.get(CONF_PUBLISH_STRINGS, False)),
    )


def create_alert_receiver(
    hass: HomeAssistant, entry: ZabbixConfigEntry, coordinator: ZabbixCoordinator
) -> AlertReceiver | None:
    """Return the alert receiver if alerts are turned on."""
    webhook_id = entry.data.get(DATA_ALERT_WEBHOOK_ID)
    secret = entry.data.get(DATA_ALERT_SECRET)
    if not entry.options.get(CONF_ALERTS) or not webhook_id or not secret:
        return None
    return AlertReceiver(
        hass, entry.title, webhook_id, secret, coordinator.async_request_refresh
    )


async def async_setup_entry(hass: HomeAssistant, entry: ZabbixConfigEntry) -> bool:
    """Set up Zabbix from a config entry."""
    client = ZabbixClient(
        entry.data[CONF_URL],
        entry.data[CONF_API_TOKEN],
        async_get_clientsession(hass, verify_ssl=entry.data[CONF_VERIFY_SSL]),
    )
    coordinator = ZabbixCoordinator(hass, entry, client)
    coordinator.publisher = create_publisher(hass, entry)
    coordinator.alerts = create_alert_receiver(hass, entry, coordinator)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if (alerts := coordinator.alerts) is not None:
        alerts.async_register()
        entry.async_on_unload(alerts.async_unregister)

    if (publisher := coordinator.publisher) is not None:

        @callback
        def _async_start_publisher(_hass: HomeAssistant) -> None:
            # Started once HA is up, so the first discovery lists every entity.
            publisher.async_start()

        entry.async_on_unload(async_at_started(hass, _async_start_publisher))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ZabbixConfigEntry) -> bool:
    """Unload a config entry."""
    if (publisher := entry.runtime_data.publisher) is not None:
        await publisher.async_stop()
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
