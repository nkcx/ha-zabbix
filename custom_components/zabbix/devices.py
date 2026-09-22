"""Device info for the Zabbix service device and host devices."""

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .api import Host
from .unique_ids import host_device_identifier, service_device_identifier


def service_device_info(entry_id: str, frontend_url: str, version: str) -> DeviceInfo:
    """Return the device info of the Zabbix (service) device."""
    return DeviceInfo(
        identifiers={service_device_identifier(entry_id)},
        name="Zabbix",
        manufacturer="Zabbix",
        model="Zabbix server",
        sw_version=version or None,
        configuration_url=frontend_url,
        entry_type=DeviceEntryType.SERVICE,
    )


def host_device_info(
    entry_id: str, frontend_url: str, host: Host, service_device_id: str | None
) -> DeviceInfo:
    """Return the device info of a host."""
    info = DeviceInfo(
        identifiers={host_device_identifier(entry_id, host.host_id)},
        name=host.name,
        manufacturer="Zabbix",
        model=host.host,
        configuration_url=(
            f"{frontend_url}zabbix.php?action=host.dashboard.view&hostid={host.host_id}"
        ),
    )
    if service_device_id is not None:
        info["via_device_id"] = service_device_id
    return info
