"""Base entities for the Zabbix integration."""

from collections.abc import Callable
from typing import override

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import Host, ItemValue
from .coordinator import TrackedItem, ZabbixCoordinator, ZabbixData
from .devices import host_device_info, service_device_info
from .unique_ids import host_unique_id, item_unique_id, service_unique_id


def _service_device_info(coordinator: ZabbixCoordinator) -> DeviceInfo:
    return service_device_info(
        coordinator.config_entry.entry_id,
        coordinator.client.frontend_url,
        coordinator.version,
    )


def _host_device_info(coordinator: ZabbixCoordinator, host: Host) -> DeviceInfo:
    return host_device_info(
        coordinator.config_entry.entry_id,
        coordinator.client.frontend_url,
        host,
        coordinator.service_device_id,
    )


class ZabbixEntity(CoordinatorEntity[ZabbixCoordinator]):
    """Base class for all Zabbix entities."""

    _attr_has_entity_name = True


class ZabbixServiceEntity(ZabbixEntity):
    """An entity on the Zabbix (service) device."""

    def __init__(self, coordinator: ZabbixCoordinator, kind: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._attr_unique_id = service_unique_id(
            coordinator.config_entry.entry_id, kind
        )
        self._attr_device_info = _service_device_info(coordinator)


class ZabbixHostEntity(ZabbixEntity):
    """A built-in entity on a host device."""

    def __init__(self, coordinator: ZabbixCoordinator, host_id: str, kind: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.host_id = host_id
        self._attr_unique_id = host_unique_id(
            coordinator.config_entry.entry_id, host_id, kind
        )
        self._attr_device_info = _host_device_info(
            coordinator, coordinator.data.hosts[host_id]
        )

    @property
    def host(self) -> Host:
        """Return the host."""
        return self.coordinator.data.hosts[self.host_id]

    @property
    @override
    def available(self) -> bool:
        """Return True while the host is in the coordinator data."""
        return super().available and self.host_id in self.coordinator.data.hosts


class ZabbixItemEntity(ZabbixEntity):
    """An entity that represents a Zabbix item."""

    def __init__(self, coordinator: ZabbixCoordinator, tracked: TrackedItem) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        item = tracked.item
        self.item_host_id = item.host_id
        self.item_key = item.key
        self._attr_unique_id = item_unique_id(
            coordinator.config_entry.entry_id, item.host_id, item.key
        )
        self._attr_name = tracked.name
        self._attr_entity_registry_enabled_default = tracked.enabled_default
        if tracked.host_id is None:
            self._attr_device_info = _service_device_info(coordinator)
        else:
            self._attr_device_info = _host_device_info(
                coordinator, coordinator.data.hosts[tracked.host_id]
            )

    @property
    def tracked(self) -> TrackedItem | None:
        """Return the tracked item, if it is still tracked."""
        return self.coordinator.data.tracked_item(self.item_host_id, self.item_key)

    @property
    def value(self) -> ItemValue | None:
        """Return the item's latest value."""
        if (tracked := self.tracked) is None:
            return None
        return self.coordinator.data.values.get(tracked.item.item_id)

    @property
    @override
    def available(self) -> bool:
        """Return True while Zabbix supports the item and it is tracked."""
        if not super().available:
            return False
        value = self.value
        return value is not None and value.supported


type EntityFactories = dict[str, Callable[[], Entity]]


@callback
def async_add_dynamic_entities(
    coordinator: ZabbixCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factories: Callable[[ZabbixData], EntityFactories],
) -> None:
    """Add entities now and whenever new hosts or items appear.

    ``factories`` returns a constructor per unique id for the current data.
    """
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        current = factories(coordinator.data)
        new = [
            factory()
            for unique_id, factory in current.items()
            if unique_id not in known
        ]
        known.intersection_update(current)
        known.update(current)
        if new:
            async_add_entities(new)

    _add_new()
    coordinator.config_entry.async_on_unload(coordinator.async_add_listener(_add_new))
