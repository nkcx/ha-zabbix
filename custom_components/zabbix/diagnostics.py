"""Diagnostics for the Zabbix integration."""

from collections import Counter
from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONF_API_TOKEN
from .coordinator import ZabbixConfigEntry
from .unique_ids import host_device_identifier

TO_REDACT = {CONF_API_TOKEN}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ZabbixConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data
    user = coordinator.token_user
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "server": {
            "version": data.version,
            "user_type": user.type.name if user else None,
            "counts": asdict(data.server_counts) if data.server_counts else None,
        },
        "hosts": len(data.hosts),
        "items": {
            "tracked": len(data.items),
            "polled": len(data.values),
            "service_device": sum(
                1 for tracked in data.items.values() if tracked.host_id is None
            ),
            "enabled_by_default": sum(
                1 for tracked in data.items.values() if tracked.enabled_default
            ),
            "value_kinds": dict(
                Counter(tracked.item.value_type.name for tracked in data.items.values())
            ),
            "unsupported": sum(
                1 for value in data.values.values() if not value.supported
            ),
        },
        "problems": {
            "total": len(data.problems),
            "shown": len(data.shown_problems()),
        },
        "last_update_success": coordinator.last_update_success,
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ZabbixConfigEntry, device: dr.DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a device."""
    data = entry.runtime_data.data
    host_id = next(
        (
            host_id
            for host_id in data.hosts
            if host_device_identifier(entry.entry_id, host_id) in device.identifiers
        ),
        None,
    )
    items = [tracked for tracked in data.items.values() if tracked.host_id == host_id]
    result: dict[str, Any] = {
        "items": [
            {
                "key": tracked.item.key,
                "name": tracked.name,
                "value_type": tracked.item.value_type.name,
                "units": tracked.item.units,
                "value_map": tracked.item.value_map.name
                if tracked.item.value_map
                else None,
                "enabled_by_default": tracked.enabled_default,
                "value": asdict(value)
                if (value := data.values.get(tracked.item.item_id))
                else None,
            }
            for tracked in items
        ],
    }
    if host_id is not None:
        host = data.hosts[host_id]
        result["host"] = {
            "host_id": host.host_id,
            "in_maintenance": host.in_maintenance,
            "interfaces": [
                {
                    "type": interface.type.name,
                    "main": interface.main,
                    "available": interface.available.name,
                    "error": interface.error,
                }
                for interface in host.interfaces
            ],
            "availability": {
                kind.value: availability.status.name
                for kind, availability in data.availability[host_id].items()
            },
        }
        result["problems"] = [
            {
                "event_id": problem.event_id,
                "severity": problem.severity.name,
                "acknowledged": problem.acknowledged,
                "suppressed": problem.suppressed,
                "symptom": problem.is_symptom,
            }
            for problem in data.problems_by_host.get(host_id, [])
        ]
    return result
