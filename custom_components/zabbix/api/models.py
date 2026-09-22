"""Typed models for Zabbix API objects.

Zabbix returns every scalar as a string. The ``from_api`` constructors convert the
fields we use into proper Python types and tolerate missing optional fields.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum
import re
from typing import Any, Self


class Severity(IntEnum):
    """Trigger/problem severity."""

    NOT_CLASSIFIED = 0
    INFORMATION = 1
    WARNING = 2
    AVERAGE = 3
    HIGH = 4
    DISASTER = 5


class Availability(IntEnum):
    """Interface availability, as used by the Zabbix frontend.

    ``MIXED`` is not stored by Zabbix; the frontend derives it when interfaces of
    one type disagree.
    """

    UNKNOWN = 0
    AVAILABLE = 1
    UNAVAILABLE = 2
    MIXED = 3


class InterfaceType(IntEnum):
    """Host interface type."""

    AGENT = 1
    SNMP = 2
    IPMI = 3
    JMX = 4


class ItemType(IntEnum):
    """Item types the integration treats specially."""

    ZABBIX_AGENT = 0
    INTERNAL = 5
    ZABBIX_ACTIVE = 7
    IPMI = 12
    JMX = 16
    DEPENDENT = 18
    SNMP = 20


class ValueType(IntEnum):
    """Item value type."""

    FLOAT = 0
    CHAR = 1
    LOG = 2
    UNSIGNED = 3
    TEXT = 4
    BINARY = 5

    @property
    def is_numeric(self) -> bool:
        """Return True for numeric value types."""
        return self in (ValueType.FLOAT, ValueType.UNSIGNED)


class UserType(IntEnum):
    """Zabbix user type."""

    USER = 1
    ADMIN = 2
    SUPER_ADMIN = 3


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except TypeError, ValueError:
        return default


def _enum[E: IntEnum](enum: type[E], value: Any, default: E) -> E:
    try:
        return enum(_int(value, default.value))
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class Tag:
    """A Zabbix tag."""

    tag: str
    value: str

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Self:
        """Create from an API object."""
        return cls(tag=str(data.get("tag", "")), value=str(data.get("value", "")))


def _tags(data: Mapping[str, Any]) -> tuple[Tag, ...]:
    return tuple(Tag.from_api(tag) for tag in data.get("tags") or ())


@dataclass(frozen=True, slots=True)
class HostGroup:
    """A host group."""

    group_id: str
    name: str
    host_count: int = 0

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Self:
        """Create from an API object."""
        return cls(
            group_id=str(data["groupid"]),
            name=str(data.get("name", "")),
            host_count=_int(data.get("hosts")),
        )


@dataclass(frozen=True, slots=True)
class HostInterface:
    """A host interface with its availability."""

    interface_id: str
    type: InterfaceType
    main: bool
    use_ip: bool
    ip: str
    dns: str
    port: str
    available: Availability
    error: str

    @property
    def address(self) -> str:
        """Return the address the way the Zabbix frontend displays it."""
        host = self.ip if self.use_ip else self.dns
        if self.use_ip and ":" in host:
            host = f"[{host}]"
        return f"{host}:{self.port}"

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Self:
        """Create from an API object."""
        return cls(
            interface_id=str(data["interfaceid"]),
            type=_enum(InterfaceType, data.get("type"), InterfaceType.AGENT),
            main=_int(data.get("main")) == 1,
            use_ip=_int(data.get("useip"), 1) == 1,
            ip=str(data.get("ip", "")),
            dns=str(data.get("dns", "")),
            port=str(data.get("port", "")),
            available=_enum(Availability, data.get("available"), Availability.UNKNOWN),
            error=str(data.get("error", "")),
        )


@dataclass(frozen=True, slots=True)
class Host:
    """A monitored host."""

    host_id: str
    host: str
    name: str
    description: str
    in_maintenance: bool
    maintenance_id: str
    active_available: Availability
    interfaces: tuple[HostInterface, ...]
    group_ids: frozenset[str]

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Self:
        """Create from an API object."""
        return cls(
            host_id=str(data["hostid"]),
            host=str(data.get("host", "")),
            name=str(data.get("name") or data.get("host", "")),
            description=str(data.get("description", "")),
            in_maintenance=_int(data.get("maintenance_status")) == 1,
            maintenance_id=str(data.get("maintenanceid", "0")),
            active_available=_enum(
                Availability, data.get("active_available"), Availability.UNKNOWN
            ),
            interfaces=tuple(
                HostInterface.from_api(interface)
                for interface in data.get("interfaces") or ()
            ),
            group_ids=frozenset(
                str(group["groupid"]) for group in data.get("hostgroups") or ()
            ),
        )


class MappingType(IntEnum):
    """Value map mapping type."""

    EQUAL = 0
    GREATER_OR_EQUAL = 1
    LESS_OR_EQUAL = 2
    RANGE = 3
    REGEX = 4
    DEFAULT = 5


_RANGE_PART = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*(?:-\s*(-?\d+(?:\.\d+)?))?\s*$")


def _float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class ValueMapping:
    """One entry of a value map."""

    type: MappingType
    value: str
    new_value: str

    def matches(self, raw: str) -> bool:
        """Return True if ``raw`` matches this mapping."""
        match self.type:
            case MappingType.EQUAL:
                if raw == self.value:
                    return True
                number, target = _float(raw), _float(self.value)
                return number is not None and number == target
            case MappingType.GREATER_OR_EQUAL | MappingType.LESS_OR_EQUAL:
                number, target = _float(raw), _float(self.value)
                if number is None or target is None:
                    return False
                if self.type is MappingType.GREATER_OR_EQUAL:
                    return number >= target
                return number <= target
            case MappingType.RANGE:
                return self._in_range(raw)
            case MappingType.REGEX:
                try:
                    return re.search(self.value, raw) is not None
                except re.error:
                    return False
            case _:
                return False

    def _in_range(self, raw: str) -> bool:
        number = _float(raw)
        if number is None:
            return False
        for part in self.value.split(","):
            if not (match := _RANGE_PART.match(part)):
                continue
            low = float(match.group(1))
            high = float(match.group(2)) if match.group(2) is not None else low
            if low <= number <= high:
                return True
        return False


@dataclass(frozen=True, slots=True)
class ValueMap:
    """A value map attached to an item."""

    value_map_id: str
    name: str
    mappings: tuple[ValueMapping, ...]

    @property
    def options(self) -> list[str]:
        """Return the distinct mapped values, in map order."""
        return list(dict.fromkeys(mapping.new_value for mapping in self.mappings))

    def map(self, raw: str) -> str | None:
        """Map a raw value like Zabbix does; None if nothing matches."""
        default: str | None = None
        for mapping in self.mappings:
            if mapping.type is MappingType.DEFAULT:
                default = mapping.new_value
            elif mapping.matches(raw):
                return mapping.new_value
        return default

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Self | None:
        """Create from an API object; None when the item has no value map."""
        if not data or not data.get("valuemapid"):
            return None
        return cls(
            value_map_id=str(data["valuemapid"]),
            name=str(data.get("name", "")),
            mappings=tuple(
                ValueMapping(
                    type=_enum(MappingType, mapping.get("type"), MappingType.EQUAL),
                    value=str(mapping.get("value", "")),
                    new_value=str(mapping.get("newvalue", "")),
                )
                for mapping in data.get("mappings") or ()
            ),
        )


@dataclass(frozen=True, slots=True)
class Item:
    """Item metadata (configuration, not the latest value)."""

    item_id: str
    host_id: str
    key: str
    name: str
    type: int
    value_type: ValueType
    units: str
    value_map: ValueMap | None
    master_item_id: str
    tags: tuple[Tag, ...] = field(default=())

    def has_tag(self, tag: str) -> bool:
        """Return True if the item carries a tag with this name."""
        return any(item_tag.tag == tag for item_tag in self.tags)

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Self:
        """Create from an API object."""
        value_map = data.get("valuemap")
        return cls(
            item_id=str(data["itemid"]),
            host_id=str(data.get("hostid", "")),
            key=str(data.get("key_", "")),
            name=str(data.get("name_resolved") or data.get("name", "")),
            type=_int(data.get("type")),
            value_type=_enum(ValueType, data.get("value_type"), ValueType.TEXT),
            units=str(data.get("units", "")),
            value_map=ValueMap.from_api(value_map)
            if isinstance(value_map, Mapping)
            else None,
            master_item_id=str(data.get("master_itemid", "0")),
            tags=_tags(data),
        )


@dataclass(frozen=True, slots=True)
class ItemValue:
    """The latest value of an item."""

    item_id: str
    last_value: str
    last_clock: int
    supported: bool
    error: str

    @property
    def has_value(self) -> bool:
        """Return True once Zabbix has collected a value."""
        return self.last_clock > 0

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Self:
        """Create from an API object."""
        return cls(
            item_id=str(data["itemid"]),
            last_value=str(data.get("lastvalue", "")),
            last_clock=_int(data.get("lastclock")),
            supported=_int(data.get("state")) == 0,
            error=str(data.get("error", "")),
        )


@dataclass(frozen=True, slots=True)
class Problem:
    """An open problem."""

    event_id: str
    trigger_id: str
    name: str
    severity: Severity
    acknowledged: bool
    suppressed: bool
    clock: int
    opdata: str
    cause_event_id: str
    tags: tuple[Tag, ...] = field(default=())
    host_ids: tuple[str, ...] = field(default=())

    @property
    def is_symptom(self) -> bool:
        """Return True if the problem is a symptom of another problem."""
        return self.cause_event_id not in ("", "0")

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Self:
        """Create from an API object."""
        return cls(
            event_id=str(data["eventid"]),
            trigger_id=str(data.get("objectid", "")),
            name=str(data.get("name", "")),
            severity=_enum(Severity, data.get("severity"), Severity.NOT_CLASSIFIED),
            acknowledged=_int(data.get("acknowledged")) == 1,
            suppressed=_int(data.get("suppressed")) == 1,
            clock=_int(data.get("clock")),
            opdata=str(data.get("opdata", "")),
            cause_event_id=str(data.get("cause_eventid", "0")),
            tags=_tags(data),
        )


@dataclass(frozen=True, slots=True)
class TokenUser:
    """The user an API token belongs to."""

    user_id: str
    username: str
    type: UserType

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Self:
        """Create from a ``user.checkAuthentication`` result."""
        return cls(
            user_id=str(data.get("userid", "")),
            username=str(data.get("username", "")),
            type=_enum(UserType, data.get("type"), UserType.USER),
        )


@dataclass(frozen=True, slots=True)
class ServerCounts:
    """Object counts shown on the Zabbix System information page."""

    hosts: int
    items: int
    items_unsupported: int
    triggers: int


@dataclass(frozen=True, slots=True)
class Maintenance:
    """A maintenance period."""

    maintenance_id: str
    name: str
    host_ids: tuple[str, ...]

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Self:
        """Create from an API object."""
        return cls(
            maintenance_id=str(data["maintenanceid"]),
            name=str(data.get("name", "")),
            host_ids=tuple(str(host["hostid"]) for host in data.get("hosts") or ()),
        )


def parse_version(version: str) -> tuple[int, int, int]:
    """Parse a version string like ``7.4.12`` or ``8.0.0alpha1``."""
    parts: list[int] = []
    for part in version.split(".")[:3]:
        digits = re.match(r"\d+", part)
        parts.append(int(digits.group()) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def ids(values: Sequence[str] | set[str] | frozenset[str]) -> list[str]:
    """Return a sorted list of ids, for stable request payloads."""
    return sorted(values, key=lambda value: (len(value), value))
