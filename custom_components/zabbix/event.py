"""Problem events for the Zabbix integration."""

from functools import partial
from typing import override

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import KIND_PROBLEM_EVENTS, ProblemEventType
from .coordinator import ProblemChange, ZabbixConfigEntry, ZabbixCoordinator, ZabbixData
from .entity import (
    EntityFactories,
    ZabbixHostEntity,
    ZabbixServiceEntity,
    async_add_dynamic_entities,
)
from .unique_ids import host_unique_id, service_unique_id

PARALLEL_UPDATES = 0

EVENT_TYPES = [event_type.value for event_type in ProblemEventType]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZabbixConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Zabbix problem event entities."""
    coordinator = entry.runtime_data
    entry_id = entry.entry_id

    def factories(data: ZabbixData) -> EntityFactories:
        result: EntityFactories = {
            service_unique_id(entry_id, KIND_PROBLEM_EVENTS): partial(
                ZabbixProblemEvents, coordinator
            )
        }
        for host_id in data.hosts:
            result[host_unique_id(entry_id, host_id, KIND_PROBLEM_EVENTS)] = partial(
                ZabbixHostProblemEvents, coordinator, host_id
            )
        return result

    async_add_dynamic_entities(coordinator, async_add_entities, factories)


def _attributes(
    coordinator: ZabbixCoordinator, change: ProblemChange
) -> dict[str, object]:
    data = coordinator.event_data(change, coordinator.data.hosts)
    data.pop("config_entry_id")
    data.pop("type")
    return data


class _ProblemEventEntity(EventEntity):
    """Triggers events for problem changes; subclasses pick the relevant ones."""

    _attr_translation_key = KIND_PROBLEM_EVENTS
    _attr_event_types = EVENT_TYPES
    coordinator: ZabbixCoordinator
    _was_available: bool | None = None

    def _relevant(self, change: ProblemChange) -> bool:
        raise NotImplementedError

    @callback
    def _handle_coordinator_update(self) -> None:
        """Trigger an event per relevant problem change."""
        for change in self.coordinator.data.changes:
            if self._relevant(change):
                self._trigger_event(change.type, _attributes(self.coordinator, change))
                self.async_write_ha_state()
        if self.available != self._was_available:
            self._was_available = self.available
            self.async_write_ha_state()


class ZabbixProblemEvents(_ProblemEventEntity, ZabbixServiceEntity):
    """Every problem change on the Zabbix server."""

    def __init__(self, coordinator: ZabbixCoordinator) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, KIND_PROBLEM_EVENTS)

    @override
    def _relevant(self, change: ProblemChange) -> bool:
        return True


class ZabbixHostProblemEvents(_ProblemEventEntity, ZabbixHostEntity):
    """Problem changes on one host."""

    def __init__(self, coordinator: ZabbixCoordinator, host_id: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, host_id, KIND_PROBLEM_EVENTS)

    @override
    def _relevant(self, change: ProblemChange) -> bool:
        return self.host_id in change.problem.host_ids
