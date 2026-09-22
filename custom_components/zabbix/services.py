"""Actions (services) for the Zabbix integration."""

from collections.abc import Awaitable, Callable, Coroutine
import time
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
import voluptuous as vol

from .api import (
    AcknowledgeAction,
    ZabbixApiError,
    ZabbixError,
    ZabbixPermissionError,
)
from .const import DOMAIN, MAINTENANCE_PREFIX
from .coordinator import ZabbixConfigEntry, ZabbixCoordinator

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_EVENT_ID = "event_id"
ATTR_MESSAGE = "message"
ATTR_DURATION = "duration"
ATTR_COLLECT_DATA = "collect_data"
ATTR_DESCRIPTION = "description"

SERVICE_ACKNOWLEDGE = "acknowledge_problem"
SERVICE_UNACKNOWLEDGE = "unacknowledge_problem"
SERVICE_CLOSE = "close_problem"
SERVICE_START_MAINTENANCE = "start_maintenance"
SERVICE_END_MAINTENANCE = "end_maintenance"

_EVENT_ID = vol.All(cv.string, vol.Match(r"^\d+$"))
_EVENT_IDS = vol.All(cv.ensure_list, [_EVENT_ID])

PROBLEM_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_EVENT_ID): _EVENT_IDS,
        vol.Optional(ATTR_MESSAGE): cv.string,
    }
)
START_MAINTENANCE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
        vol.Required(ATTR_DURATION): vol.All(cv.time_period, cv.positive_timedelta),
        vol.Optional(ATTR_COLLECT_DATA, default=True): cv.boolean,
        vol.Optional(ATTR_DESCRIPTION, default=""): cv.string,
    }
)
END_MAINTENANCE_SCHEMA = vol.Schema(
    {vol.Required(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string])}
)

# Zabbix limits maintenance names to 128 characters.
_MAX_MAINTENANCE_NAME = 128


def _coordinator(hass: HomeAssistant, entry_id: str) -> ZabbixCoordinator:
    entry: ZabbixConfigEntry | None = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_found",
            translation_placeholders={"entry_id": entry_id},
        )
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_loaded",
            translation_placeholders={"title": entry.title},
        )
    return entry.runtime_data


def _hosts_by_coordinator(
    hass: HomeAssistant, device_ids: list[str]
) -> dict[ZabbixCoordinator, set[str]]:
    """Resolve host devices to their coordinator and Zabbix host id."""
    registry = dr.async_get(hass)
    result: dict[ZabbixCoordinator, set[str]] = {}
    for device_id in device_ids:
        device = registry.async_get(device_id)
        host: tuple[ZabbixCoordinator, str] | None = None
        if device is not None:
            for domain, identifier in device.identifiers:
                if domain != DOMAIN or "_" not in identifier:
                    continue
                entry_id, host_id = identifier.split("_", 1)
                if entry_id in device.config_entries:
                    host = (_coordinator(hass, entry_id), host_id)
        if host is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_a_host",
                translation_placeholders={"device_id": device_id},
            )
        result.setdefault(host[0], set()).add(host[1])
    return result


async def _call(coordinator: ZabbixCoordinator, action: Awaitable[Any]) -> Any:
    """Run a write call, translating errors, then refresh the data."""
    try:
        result = await action
    except ZabbixPermissionError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="permission_denied",
            translation_placeholders={"error": err.data},
        ) from err
    except ZabbixApiError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="api_error",
            translation_placeholders={"error": err.data or err.message},
        ) from err
    except ZabbixError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        ) from err
    await coordinator.async_request_refresh()
    return result


def _problem_handler(
    action: AcknowledgeAction,
) -> Callable[[ServiceCall], Coroutine[Any, Any, None]]:
    async def handle(call: ServiceCall) -> None:
        coordinator = _coordinator(call.hass, call.data[ATTR_CONFIG_ENTRY_ID])
        await _call(
            coordinator,
            coordinator.client.async_acknowledge(
                call.data[ATTR_EVENT_ID],
                action,
                message=call.data.get(ATTR_MESSAGE),
            ),
        )

    return handle


async def _start_maintenance(call: ServiceCall) -> None:
    start = int(time.time())
    duration = int(call.data[ATTR_DURATION].total_seconds())
    for coordinator, host_ids in _hosts_by_coordinator(
        call.hass, call.data[ATTR_DEVICE_ID]
    ).items():
        hosts = coordinator.data.hosts
        names = ", ".join(
            sorted(
                hosts[host_id].name if host_id in hosts else host_id
                for host_id in host_ids
            )
        )
        suffix = f" ({start})"
        name = f"{MAINTENANCE_PREFIX}{names}"
        name = name[: _MAX_MAINTENANCE_NAME - len(suffix)] + suffix
        await _call(
            coordinator,
            coordinator.client.async_create_maintenance(
                name,
                host_ids,
                start=start,
                duration=duration,
                collect_data=call.data[ATTR_COLLECT_DATA],
                description=call.data[ATTR_DESCRIPTION],
            ),
        )


async def _end_maintenance(call: ServiceCall) -> None:
    for coordinator, host_ids in _hosts_by_coordinator(
        call.hass, call.data[ATTR_DEVICE_ID]
    ).items():
        maintenances = await _call(
            coordinator, coordinator.client.async_get_maintenances(MAINTENANCE_PREFIX)
        )
        to_delete: list[str] = []
        for maintenance in maintenances:
            if not host_ids & set(maintenance.host_ids):
                continue
            remaining = [
                host_id for host_id in maintenance.host_ids if host_id not in host_ids
            ]
            if remaining:
                await _call(
                    coordinator,
                    coordinator.client.async_set_maintenance_hosts(
                        maintenance.maintenance_id, remaining
                    ),
                )
            else:
                to_delete.append(maintenance.maintenance_id)
        if to_delete:
            await _call(
                coordinator, coordinator.client.async_delete_maintenances(to_delete)
            )


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the Zabbix actions."""
    for service, action in (
        (SERVICE_ACKNOWLEDGE, AcknowledgeAction.ACKNOWLEDGE),
        (SERVICE_UNACKNOWLEDGE, AcknowledgeAction.UNACKNOWLEDGE),
        (SERVICE_CLOSE, AcknowledgeAction.CLOSE),
    ):
        hass.services.async_register(
            DOMAIN, service, _problem_handler(action), schema=PROBLEM_SCHEMA
        )
    hass.services.async_register(
        DOMAIN,
        SERVICE_START_MAINTENANCE,
        _start_maintenance,
        schema=START_MAINTENANCE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_END_MAINTENANCE,
        _end_maintenance,
        schema=END_MAINTENANCE_SCHEMA,
    )
