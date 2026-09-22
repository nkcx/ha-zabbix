"""Sensors for the Zabbix integration."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Any, override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from .api import Availability, Item, Problem, ServerCounts
from .availability import AvailabilityKind
from .const import (
    KIND_AVAILABILITY_PREFIX,
    KIND_HIGHEST_SEVERITY,
    KIND_HOSTS,
    KIND_ITEMS,
    KIND_PROBLEMS,
    KIND_TRIGGERS,
    KIND_UNSUPPORTED_ITEMS,
    KIND_VERSION,
    SEVERITY_NAMES,
)
from .coordinator import TrackedItem, ZabbixConfigEntry, ZabbixCoordinator, ZabbixData
from .entity import (
    EntityFactories,
    ZabbixHostEntity,
    ZabbixItemEntity,
    ZabbixServiceEntity,
    async_add_dynamic_entities,
)
from .representation import Representation, ValueKind, convert, representation_for
from .unique_ids import host_unique_id, item_unique_id, service_unique_id

PARALLEL_UPDATES = 0

AVAILABILITY_STATES = {
    Availability.UNKNOWN: "unknown",
    Availability.AVAILABLE: "available",
    Availability.UNAVAILABLE: "not_available",
    Availability.MIXED: "mixed",
}
SEVERITY_OPTIONS = ["none", *SEVERITY_NAMES]
AVAILABILITY_OPTIONS = list(AVAILABILITY_STATES.values())
# Boot time is computed from uptime and the collection time, so it jitters a bit.
BOOT_TIME_TOLERANCE = timedelta(seconds=60)


def problem_summary(problem: Problem, data: ZabbixData) -> dict[str, Any]:
    """Return a compact description of a problem for attributes."""
    return {
        "event_id": problem.event_id,
        "name": problem.name,
        "severity": SEVERITY_NAMES[problem.severity],
        "acknowledged": problem.acknowledged,
        "since": dt_util.utc_from_timestamp(problem.clock).isoformat(),
        "hosts": [
            data.hosts[host_id].name
            for host_id in problem.host_ids
            if host_id in data.hosts
        ],
    }


def severity_counts(problems: list[Problem]) -> dict[str, int]:
    """Return the number of problems per severity."""
    counts = dict.fromkeys(SEVERITY_NAMES, 0)
    for problem in problems:
        counts[SEVERITY_NAMES[problem.severity]] += 1
    return counts


@dataclass(frozen=True, kw_only=True)
class ServiceSensorDescription(SensorEntityDescription):
    """Describes a sensor on the Zabbix device."""

    value_fn: Callable[[ZabbixData], StateType]


def _count(field: str) -> Callable[[ZabbixData], StateType]:
    def value(data: ZabbixData) -> StateType:
        counts: ServerCounts | None = data.server_counts
        return None if counts is None else int(getattr(counts, field))

    return value


VERSION_SENSOR = ServiceSensorDescription(
    key=KIND_VERSION,
    translation_key=KIND_VERSION,
    entity_category=EntityCategory.DIAGNOSTIC,
    value_fn=lambda data: data.version,
)
COUNT_SENSORS = (
    ServiceSensorDescription(
        key=KIND_HOSTS,
        translation_key=KIND_HOSTS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_count("hosts"),
    ),
    ServiceSensorDescription(
        key=KIND_ITEMS,
        translation_key=KIND_ITEMS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_count("items"),
    ),
    ServiceSensorDescription(
        key=KIND_UNSUPPORTED_ITEMS,
        translation_key=KIND_UNSUPPORTED_ITEMS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_count("items_unsupported"),
    ),
    ServiceSensorDescription(
        key=KIND_TRIGGERS,
        translation_key=KIND_TRIGGERS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_count("triggers"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZabbixConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Zabbix sensors."""
    coordinator = entry.runtime_data
    entry_id = entry.entry_id

    def factories(data: ZabbixData) -> EntityFactories:
        result: EntityFactories = {
            service_unique_id(entry_id, KIND_PROBLEMS): partial(
                ZabbixProblemsSensor, coordinator
            ),
            service_unique_id(entry_id, KIND_VERSION): partial(
                ZabbixServiceSensor, coordinator, VERSION_SENSOR
            ),
        }
        if data.server_counts is not None:
            for description in COUNT_SENSORS:
                result[service_unique_id(entry_id, description.key)] = partial(
                    ZabbixServiceSensor, coordinator, description
                )
        for host_id, availability in data.availability.items():
            result[host_unique_id(entry_id, host_id, KIND_HIGHEST_SEVERITY)] = partial(
                ZabbixHighestSeveritySensor, coordinator, host_id
            )
            for kind in availability:
                result[
                    host_unique_id(
                        entry_id, host_id, f"{KIND_AVAILABILITY_PREFIX}{kind}"
                    )
                ] = partial(ZabbixAvailabilitySensor, coordinator, host_id, kind)
        for tracked in data.items.values():
            item = tracked.item
            result[item_unique_id(entry_id, item.host_id, item.key)] = partial(
                ZabbixItemSensor, coordinator, tracked
            )
        return result

    async_add_dynamic_entities(coordinator, async_add_entities, factories)


class ZabbixServiceSensor(ZabbixServiceEntity, SensorEntity):
    """A sensor about the Zabbix server."""

    entity_description: ServiceSensorDescription

    def __init__(
        self, coordinator: ZabbixCoordinator, description: ServiceSensorDescription
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    @override
    def native_value(self) -> StateType:
        """Return the value."""
        return self.entity_description.value_fn(self.coordinator.data)


class ZabbixProblemsSensor(ZabbixServiceEntity, SensorEntity):
    """The number of problems Zabbix shows."""

    _attr_translation_key = KIND_PROBLEMS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset(
        {"problems", "suppressed", "symptoms", *SEVERITY_NAMES}
    )

    def __init__(self, coordinator: ZabbixCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, KIND_PROBLEMS)

    @property
    @override
    def native_value(self) -> int:
        """Return the number of problems."""
        return len(self.coordinator.data.shown_problems())

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return problem details."""
        data = self.coordinator.data
        shown = data.shown_problems()
        return {
            **severity_counts(shown),
            "suppressed": sum(
                1 for problem in data.problems.values() if problem.suppressed
            ),
            "symptoms": sum(
                1 for problem in data.problems.values() if problem.is_symptom
            ),
            "problems": [problem_summary(problem, data) for problem in shown],
        }


class ZabbixHighestSeveritySensor(ZabbixHostEntity, SensorEntity):
    """The highest severity among a host's problems."""

    _attr_translation_key = KIND_HIGHEST_SEVERITY
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = SEVERITY_OPTIONS

    def __init__(self, coordinator: ZabbixCoordinator, host_id: str) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, host_id, KIND_HIGHEST_SEVERITY)

    @property
    @override
    def native_value(self) -> str:
        """Return the highest severity, or none."""
        problems = self.coordinator.data.shown_problems(self.host_id)
        if not problems:
            return "none"
        return SEVERITY_NAMES[max(problem.severity for problem in problems)]


class ZabbixAvailabilitySensor(ZabbixHostEntity, SensorEntity):
    """Availability of one interface type, as shown by Zabbix."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = AVAILABILITY_OPTIONS
    _unrecorded_attributes = frozenset({"interfaces"})

    def __init__(
        self, coordinator: ZabbixCoordinator, host_id: str, kind: AvailabilityKind
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, host_id, f"{KIND_AVAILABILITY_PREFIX}{kind}")
        self.kind = kind
        self._attr_translation_key = f"{KIND_AVAILABILITY_PREFIX}{kind}"

    @property
    @override
    def available(self) -> bool:
        """Return True while the host has interfaces of this type."""
        return (
            super().available
            and self.kind in self.coordinator.data.availability[self.host_id]
        )

    @property
    @override
    def native_value(self) -> str:
        """Return the availability."""
        status = self.coordinator.data.availability[self.host_id][self.kind].status
        return AVAILABILITY_STATES[status]

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the status of each interface."""
        availability = self.coordinator.data.availability[self.host_id][self.kind]
        return {
            "interfaces": [
                {
                    "interface": interface.address,
                    "status": AVAILABILITY_STATES[interface.available],
                    "error": interface.error,
                }
                for interface in availability.interfaces
            ]
        }


class ZabbixItemSensor(ZabbixItemEntity, SensorEntity):
    """A Zabbix item."""

    _unrecorded_attributes = frozenset({"raw_value"})

    def __init__(self, coordinator: ZabbixCoordinator, tracked: TrackedItem) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, tracked)
        self._item: Item = tracked.item
        self._representation: Representation = representation_for(tracked.item)
        self._apply_representation()
        self._boot_time: datetime | None = None

    def _apply_representation(self) -> None:
        rep = self._representation
        self._attr_device_class = rep.device_class
        self._attr_native_unit_of_measurement = rep.unit
        self._attr_state_class = rep.state_class
        self._attr_options = list(rep.options) if rep.options is not None else None

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        """Follow metadata changes (units, value map) made in Zabbix."""
        if (tracked := self.tracked) is not None and tracked.item != self._item:
            self._item = tracked.item
            self._representation = representation_for(tracked.item)
            self._apply_representation()
        super()._handle_coordinator_update()

    @property
    @override
    def native_value(self) -> StateType | datetime:
        """Return the item's value."""
        if (value := self.value) is None:
            return None
        state = convert(self._representation, self._item, value).state
        if self._representation.kind is ValueKind.BOOT_TIME and isinstance(
            state, datetime
        ):
            if (
                self._boot_time is not None
                and abs(state - self._boot_time) < BOOT_TIME_TOLERANCE
            ):
                return self._boot_time
            self._boot_time = state
        return state

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the raw value when it could not be shown as the state."""
        if (value := self.value) is None:
            return None
        raw = convert(self._representation, self._item, value).raw
        return None if raw is None else {"raw_value": raw}
