"""Data update coordinator for the Zabbix integration."""

import asyncio
from dataclasses import dataclass, field, replace
from datetime import timedelta
from functools import cache
import re
import time
from typing import override

from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    Host,
    Item,
    ItemValue,
    Problem,
    ServerCounts,
    TokenUser,
    ValueType,
    ZabbixAuthError,
    ZabbixClient,
    ZabbixError,
    parse_version,
)
from .availability import AvailabilityKind, KindAvailability, host_availability
from .const import (
    CONF_GROUP_IDS,
    CONF_ITEM_MODE,
    CONF_TAG,
    DEFAULT_ITEM_MODE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TAG,
    DOMAIN,
    EVENT_PROBLEM,
    KIND_AVAILABILITY_PREFIX,
    KIND_HIGHEST_SEVERITY,
    KIND_MAINTENANCE,
    KIND_PROBLEM,
    KIND_PROBLEM_EVENTS,
    KIND_PROBLEMS,
    KIND_VERSION,
    LOGGER,
    METADATA_REFRESH_INTERVAL,
    MIN_ZABBIX_VERSION,
    SERVER_ITEMS_ENABLED_BY_DEFAULT,
    SERVER_ITEMS_SKIPPED,
    SERVICE_COUNT_KINDS,
    SEVERITY_NAMES,
    ItemMode,
    ProblemEventType,
)
from .devices import service_device_info
from .unique_ids import (
    host_device_identifier,
    host_unique_id,
    item_unique_id,
    service_device_identifier,
    service_unique_id,
)

type ZabbixConfigEntry = ConfigEntry[ZabbixCoordinator]

ISSUE_MISSING_GROUPS = "missing_groups"


@cache
def _key_pattern(pattern: str) -> re.Pattern[str]:
    return re.compile(".*".join(re.escape(part) for part in pattern.split("*")))


def key_matches(key: str, patterns: tuple[str, ...]) -> bool:
    """Return True if an item key matches a pattern; only ``*`` is a wildcard.

    Item keys contain brackets, so shell-style globs (``fnmatch``) don't work.
    """
    return any(_key_pattern(pattern).fullmatch(key) for pattern in patterns)


# Entities the integration enabled because the item mode asked for it, so that it
# can disable them again when the mode changes back.
DATA_MODE_ENABLED = "mode_enabled"


@dataclass(frozen=True, slots=True)
class TrackedItem:
    """An item that is represented by an entity."""

    item: Item
    host_id: str | None
    """Host device the entity belongs to; None for the Zabbix (service) device."""
    name: str
    enabled_default: bool


@dataclass(frozen=True, slots=True)
class ProblemChange:
    """A change to a problem between two polls."""

    type: ProblemEventType
    problem: Problem


@dataclass(slots=True)
class ZabbixData:
    """Everything the entities need."""

    version: str
    hosts: dict[str, Host]
    availability: dict[str, dict[AvailabilityKind, KindAvailability]]
    items: dict[str, TrackedItem]
    values: dict[str, ItemValue]
    problems: dict[str, Problem]
    problems_by_host: dict[str, list[Problem]]
    server_counts: ServerCounts | None
    visible_trigger_ids: frozenset[str] = field(default=frozenset())
    changes: tuple[ProblemChange, ...] = field(default=())
    item_ids_by_key: dict[tuple[str, str], str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Index items by (host id, key), which is stable across LLD re-creation."""
        self.item_ids_by_key = {
            (tracked.item.host_id, tracked.item.key): item_id
            for item_id, tracked in self.items.items()
        }

    def tracked_item(self, host_id: str, key: str) -> TrackedItem | None:
        """Return the tracked item with this host and key."""
        if (item_id := self.item_ids_by_key.get((host_id, key))) is None:
            return None
        return self.items[item_id]

    def is_shown(self, problem: Problem) -> bool:
        """Return True if Zabbix shows the problem in its default views.

        The Zabbix frontend (Problems page, host list, dashboard widgets) hides
        suppressed and symptom problems, problems of disabled triggers or
        unmonitored hosts, and problems of triggers that depend on a trigger in
        problem state.
        """
        return (
            not problem.suppressed
            and not problem.is_symptom
            and problem.trigger_id in self.visible_trigger_ids
        )

    def shown_problems(self, host_id: str | None = None) -> list[Problem]:
        """Return the problems Zabbix shows by default, for one host or all."""
        problems = (
            self.problems.values()
            if host_id is None
            else self.problems_by_host.get(host_id, [])
        )
        return [problem for problem in problems if self.is_shown(problem)]


class ZabbixCoordinator(DataUpdateCoordinator[ZabbixData]):
    """Polls Zabbix: item metadata on a slow cycle, state on every update."""

    config_entry: ZabbixConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ZabbixConfigEntry, client: ZabbixClient
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.client = client
        self.token_user: TokenUser | None = None
        self.version = ""
        self.service_device_id: str | None = None
        self._metadata_refreshed: float | None = None
        self._known_host_ids: frozenset[str] = frozenset()
        self._items: dict[str, TrackedItem] = {}
        self._value_item_ids: set[str] = set()
        self._interface_counts: dict[str, int] = {}
        self._active_counts: dict[str, int] = {}
        self._server_counts: ServerCounts | None = None
        self._trigger_hosts: dict[str, tuple[str, ...]] = {}

    @property
    def group_ids(self) -> list[str]:
        """Return the selected host groups."""
        return list(self.config_entry.options.get(CONF_GROUP_IDS, []))

    @property
    def item_mode(self) -> ItemMode:
        """Return the item mode."""
        return ItemMode(
            self.config_entry.options.get(CONF_ITEM_MODE, DEFAULT_ITEM_MODE)
        )

    @property
    def tag(self) -> str:
        """Return the opt-in tag name."""
        return str(self.config_entry.options.get(CONF_TAG, DEFAULT_TAG))

    @override
    async def _async_setup(self) -> None:
        """Check the server version and the token before the first update."""
        try:
            self.version = await self.client.async_get_version()
            self.token_user = await self.client.async_get_token_user()
        except ZabbixAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err
        except ZabbixError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        if parse_version(self.version) < MIN_ZABBIX_VERSION:
            raise ConfigEntryError(
                translation_domain=DOMAIN,
                translation_key="unsupported_version",
                translation_placeholders={"version": self.version},
            )
        # Register the Zabbix device first so host devices can link to it.
        self.service_device_id = (
            dr.async_get(self.hass)
            .async_get_or_create(
                config_entry_id=self.config_entry.entry_id,
                **service_device_info(
                    self.config_entry.entry_id,
                    self.client.frontend_url,
                    self.version,
                ),
            )
            .id
        )

    @override
    async def _async_update_data(self) -> ZabbixData:
        """Fetch the latest state from Zabbix."""
        try:
            hosts = {
                host.host_id: host
                for host in await self.client.async_get_hosts(self.group_ids)
            }
            metadata_refreshed = False
            if (
                self._metadata_refreshed is None
                or time.monotonic() - self._metadata_refreshed
                >= METADATA_REFRESH_INTERVAL.total_seconds()
                or frozenset(hosts) != self._known_host_ids
            ):
                await self._async_refresh_metadata(hosts)
                metadata_refreshed = True
            values, problem_list = await asyncio.gather(
                self.client.async_get_item_values(self._value_item_ids),
                self.client.async_get_problems(),
            )
            problem_trigger_ids = {problem.trigger_id for problem in problem_list}
            unknown_triggers = problem_trigger_ids - self._trigger_hosts.keys()
            if unknown_triggers:
                self._trigger_hosts.update(
                    await self.client.async_get_trigger_hosts(unknown_triggers)
                )
            # Dependencies and trigger/host status change at any time: every poll.
            visible_trigger_ids = await self.client.async_get_visible_trigger_ids(
                problem_trigger_ids
            )
        except ZabbixAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err
        except ZabbixError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        trigger_ids = {problem.trigger_id for problem in problem_list}
        self._trigger_hosts = {
            trigger_id: host_ids
            for trigger_id, host_ids in self._trigger_hosts.items()
            if trigger_id in trigger_ids
        }
        problems: dict[str, Problem] = {}
        problems_by_host: dict[str, list[Problem]] = {}
        for raw_problem in problem_list:
            problem = replace(
                raw_problem,
                host_ids=self._trigger_hosts.get(raw_problem.trigger_id, ()),
            )
            problems[problem.event_id] = problem
            for host_id in problem.host_ids:
                problems_by_host.setdefault(host_id, []).append(problem)

        data = ZabbixData(
            version=self.version,
            hosts=hosts,
            availability={
                host_id: host_availability(
                    host,
                    self._interface_counts,
                    self._active_counts.get(host_id, 0),
                )
                for host_id, host in hosts.items()
            },
            items=self._items,
            values=values,
            problems=problems,
            problems_by_host=problems_by_host,
            server_counts=self._server_counts,
            visible_trigger_ids=frozenset(visible_trigger_ids),
        )
        if self.data is not None:
            data.changes = _problem_changes(self.data.problems, problems)
            self._fire_problem_events(data)
        if metadata_refreshed:
            self._remove_stale(data)
        return data

    async def _async_refresh_metadata(self, hosts: dict[str, Host]) -> None:
        """Refresh the item selection and everything else that changes rarely."""
        mode = self.item_mode
        tag = self.tag
        host_ids = set(hosts)
        interface_ids = {
            interface.interface_id
            for host in hosts.values()
            for interface in host.interfaces
        }
        server_items: list[Item] = []
        server_counts: ServerCounts | None = None
        if mode is ItemMode.ALL:
            host_items = await self.client.async_get_items(host_ids=host_ids)
        else:
            host_items = await self.client.async_get_items(host_ids=host_ids, tag=tag)
        if mode is not ItemMode.TAGGED:
            server_items = await self.client.async_get_server_items()
            server_counts = await self.client.async_get_server_counts()
        (
            self._interface_counts,
            self._active_counts,
            self.version,
            groups,
        ) = await asyncio.gather(
            self.client.async_get_interface_item_counts(interface_ids),
            self.client.async_get_active_item_counts(host_ids),
            self.client.async_get_version(),
            self.client.async_get_host_groups(self.group_ids),
        )

        server_host_ids = {item.host_id for item in server_items}
        server_host_names: dict[str, str] = {}
        if len(server_host_ids) > 1:
            server_host_names = await self.client.async_get_host_names(server_host_ids)

        tracked: dict[str, TrackedItem] = {}
        for item in server_items:
            if item.value_type is ValueType.BINARY or key_matches(
                item.key, SERVER_ITEMS_SKIPPED
            ):
                continue
            name = item.name
            if host_name := server_host_names.get(item.host_id):
                name = f"{host_name}: {name}"
            tracked[item.item_id] = TrackedItem(
                item=item,
                host_id=None,
                name=name,
                enabled_default=mode is ItemMode.ALL
                or item.has_tag(tag)
                or key_matches(item.key, SERVER_ITEMS_ENABLED_BY_DEFAULT),
            )
        for item in host_items:
            if item.item_id in tracked or item.value_type is ValueType.BINARY:
                continue
            if item.host_id not in hosts:
                continue
            tracked[item.item_id] = TrackedItem(
                item=item, host_id=item.host_id, name=item.name, enabled_default=True
            )

        self._items = tracked
        self._server_counts = server_counts
        self._value_item_ids = self._enabled_item_ids(tracked)
        self._known_host_ids = frozenset(host_ids)
        self._metadata_refreshed = time.monotonic()
        self._update_missing_groups_issue({group.group_id for group in groups})
        self._update_service_device()

    def _enabled_item_ids(self, tracked: dict[str, TrackedItem]) -> set[str]:
        """Return the items whose entity is enabled (only those are polled).

        Also applies item mode changes to existing entities: entities the
        integration disabled are enabled when the mode now enables them, and
        disabled again when the mode no longer does (unless the user changed them).
        """
        registry = er.async_get(self.hass)
        entry_id = self.config_entry.entry_id
        mode_enabled = set(self.config_entry.data.get(DATA_MODE_ENABLED, []))
        still_mode_enabled: set[str] = set()
        enabled: set[str] = set()
        for item_id, tracked_item in tracked.items():
            unique_id = item_unique_id(
                entry_id, tracked_item.item.host_id, tracked_item.item.key
            )
            entity_id = registry.async_get_entity_id(SENSOR_DOMAIN, DOMAIN, unique_id)
            entry = registry.async_get(entity_id) if entity_id else None
            if entry is None:
                if tracked_item.enabled_default:
                    enabled.add(item_id)
                continue
            if tracked_item.enabled_default:
                if entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION:
                    entry = registry.async_update_entity(
                        entry.entity_id, disabled_by=None
                    )
                    still_mode_enabled.add(unique_id)
                elif unique_id in mode_enabled and entry.disabled_by is None:
                    still_mode_enabled.add(unique_id)
            elif unique_id in mode_enabled and entry.disabled_by is None:
                entry = registry.async_update_entity(
                    entry.entity_id,
                    disabled_by=er.RegistryEntryDisabler.INTEGRATION,
                )
            if entry.disabled_by is None:
                enabled.add(item_id)
        if still_mode_enabled != mode_enabled:
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={
                    **self.config_entry.data,
                    DATA_MODE_ENABLED: sorted(still_mode_enabled),
                },
            )
        return enabled

    def _update_missing_groups_issue(self, existing: set[str]) -> None:
        """Raise a repair issue when selected host groups no longer exist."""
        issue_id = f"{ISSUE_MISSING_GROUPS}_{self.config_entry.entry_id}"
        missing = [group_id for group_id in self.group_ids if group_id not in existing]
        if missing:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_MISSING_GROUPS,
                translation_placeholders={
                    "title": self.config_entry.title,
                    "count": str(len(missing)),
                },
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    def _update_service_device(self) -> None:
        """Keep the Zabbix device's software version current."""
        registry = dr.async_get(self.hass)
        device = registry.async_get_device_by_identifier(
            service_device_identifier(self.config_entry.entry_id),
            self.config_entry.entry_id,
        )
        if device is not None and device.sw_version != self.version:
            registry.async_update_device(device.id, sw_version=self.version)

    def expected_unique_ids(self, data: ZabbixData) -> set[str]:
        """Return the unique ids of every entity that should exist."""
        entry_id = self.config_entry.entry_id
        expected = {
            service_unique_id(entry_id, kind)
            for kind in (KIND_PROBLEMS, KIND_VERSION, KIND_PROBLEM_EVENTS)
        }
        if data.server_counts is not None:
            expected.update(
                service_unique_id(entry_id, kind) for kind in SERVICE_COUNT_KINDS
            )
        for host_id in data.hosts:
            expected.update(
                host_unique_id(entry_id, host_id, kind)
                for kind in (
                    KIND_PROBLEM,
                    KIND_MAINTENANCE,
                    KIND_HIGHEST_SEVERITY,
                    KIND_PROBLEM_EVENTS,
                )
            )
            expected.update(
                host_unique_id(entry_id, host_id, f"{KIND_AVAILABILITY_PREFIX}{kind}")
                for kind in data.availability[host_id]
            )
        expected.update(
            item_unique_id(entry_id, tracked.item.host_id, tracked.item.key)
            for tracked in data.items.values()
        )
        return expected

    @callback
    def _remove_stale(self, data: ZabbixData) -> None:
        """Remove devices and entities for hosts and items that are gone."""
        entry_id = self.config_entry.entry_id
        device_registry = dr.async_get(self.hass)
        keep_devices = {service_device_identifier(entry_id)} | {
            host_device_identifier(entry_id, host_id) for host_id in data.hosts
        }
        for device in dr.async_entries_for_config_entry(device_registry, entry_id):
            if not device.identifiers & keep_devices:
                LOGGER.debug("Removing stale device %s", device.name)
                device_registry.async_update_device(
                    device.id, remove_config_entry_id=entry_id
                )
        entity_registry = er.async_get(self.hass)
        expected = self.expected_unique_ids(data)
        for entity in er.async_entries_for_config_entry(entity_registry, entry_id):
            if entity.unique_id not in expected:
                LOGGER.debug("Removing stale entity %s", entity.entity_id)
                entity_registry.async_remove(entity.entity_id)

    @callback
    def _fire_problem_events(self, data: ZabbixData) -> None:
        """Fire an event on the bus for every problem change."""
        for change in data.changes:
            self.hass.bus.async_fire(EVENT_PROBLEM, self.event_data(change, data.hosts))

    def event_data(
        self, change: ProblemChange, hosts: dict[str, Host]
    ) -> dict[str, object]:
        """Return the event payload for a problem change."""
        problem = change.problem
        return {
            "config_entry_id": self.config_entry.entry_id,
            "type": change.type.value,
            "event_id": problem.event_id,
            "trigger_id": problem.trigger_id,
            "name": problem.name,
            "severity": SEVERITY_NAMES[problem.severity],
            "acknowledged": problem.acknowledged,
            "suppressed": problem.suppressed,
            "symptom": problem.is_symptom,
            "clock": problem.clock,
            "opdata": problem.opdata,
            "tags": {tag.tag: tag.value for tag in problem.tags},
            "host_ids": list(problem.host_ids),
            "hosts": [
                hosts[host_id].name for host_id in problem.host_ids if host_id in hosts
            ],
        }


def _problem_changes(
    old: dict[str, Problem], new: dict[str, Problem]
) -> tuple[ProblemChange, ...]:
    """Return the changes between two sets of open problems."""
    changes: list[ProblemChange] = [
        ProblemChange(ProblemEventType.PROBLEM, problem)
        for event_id, problem in new.items()
        if event_id not in old
    ]
    changes.extend(
        ProblemChange(ProblemEventType.RESOLVED, problem)
        for event_id, problem in old.items()
        if event_id not in new
    )
    for event_id, problem in new.items():
        if (previous := old.get(event_id)) is None:
            continue
        if problem.acknowledged != previous.acknowledged:
            changes.append(
                ProblemChange(
                    ProblemEventType.ACKNOWLEDGED
                    if problem.acknowledged
                    else ProblemEventType.UNACKNOWLEDGED,
                    problem,
                )
            )
        if problem.severity != previous.severity:
            changes.append(ProblemChange(ProblemEventType.SEVERITY_CHANGED, problem))
        if problem.suppressed != previous.suppressed:
            changes.append(
                ProblemChange(
                    ProblemEventType.SUPPRESSED
                    if problem.suppressed
                    else ProblemEventType.UNSUPPRESSED,
                    problem,
                )
            )
    return tuple(changes)
