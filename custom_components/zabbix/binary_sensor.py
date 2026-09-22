"""Binary sensors for the Zabbix integration."""

from functools import partial
from typing import Any, override

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import KIND_MAINTENANCE, KIND_PROBLEM
from .coordinator import ZabbixConfigEntry, ZabbixCoordinator, ZabbixData
from .entity import EntityFactories, ZabbixHostEntity, async_add_dynamic_entities
from .sensor import problem_summary, severity_counts
from .unique_ids import host_unique_id

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZabbixConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Zabbix binary sensors."""
    coordinator = entry.runtime_data
    entry_id = entry.entry_id

    def factories(data: ZabbixData) -> EntityFactories:
        result: EntityFactories = {}
        for host_id in data.hosts:
            result[host_unique_id(entry_id, host_id, KIND_PROBLEM)] = partial(
                ZabbixHostProblemSensor, coordinator, host_id
            )
            result[host_unique_id(entry_id, host_id, KIND_MAINTENANCE)] = partial(
                ZabbixMaintenanceSensor, coordinator, host_id
            )
        return result

    async_add_dynamic_entities(coordinator, async_add_entities, factories)


class ZabbixHostProblemSensor(ZabbixHostEntity, BinarySensorEntity):
    """On when Zabbix shows a problem on the host."""

    _attr_translation_key = KIND_PROBLEM
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _unrecorded_attributes = frozenset({"problems", "count"})

    def __init__(self, coordinator: ZabbixCoordinator, host_id: str) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, host_id, KIND_PROBLEM)

    @property
    @override
    def is_on(self) -> bool:
        """Return True if the host has a problem."""
        return bool(self.coordinator.data.shown_problems(self.host_id))

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the host's problems."""
        data = self.coordinator.data
        problems = data.shown_problems(self.host_id)
        return {
            "count": len(problems),
            **severity_counts(problems),
            "problems": [problem_summary(problem, data) for problem in problems],
        }


class ZabbixMaintenanceSensor(ZabbixHostEntity, BinarySensorEntity):
    """On when the host is in a maintenance period."""

    _attr_translation_key = KIND_MAINTENANCE
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ZabbixCoordinator, host_id: str) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, host_id, KIND_MAINTENANCE)

    @property
    @override
    def is_on(self) -> bool:
        """Return True if the host is in maintenance."""
        return self.host.in_maintenance
