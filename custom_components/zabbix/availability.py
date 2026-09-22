"""Host availability, computed exactly like the Zabbix frontend.

Zabbix has no single "host is up" value. The frontend's Availability column shows
one status per interface type (ZBX, SNMP, IPMI, JMX); active agent checks are
folded into ZBX. This mirrors ``CHostAvailability`` and
``getInterfaceAvailabilityStatus()`` from the Zabbix 7.0/7.4 frontend.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from .api import Availability, Host, InterfaceType


class AvailabilityKind(StrEnum):
    """The interface types shown in the Zabbix Availability column."""

    AGENT = "agent"
    SNMP = "snmp"
    IPMI = "ipmi"
    JMX = "jmx"


_KIND_BY_TYPE = {
    InterfaceType.AGENT: AvailabilityKind.AGENT,
    InterfaceType.SNMP: AvailabilityKind.SNMP,
    InterfaceType.IPMI: AvailabilityKind.IPMI,
    InterfaceType.JMX: AvailabilityKind.JMX,
}

ACTIVE_CHECKS = "Active checks"


@dataclass(frozen=True, slots=True)
class InterfaceStatus:
    """Availability of one interface (or of the host's active checks)."""

    address: str
    available: Availability
    error: str
    has_enabled_items: bool


@dataclass(frozen=True, slots=True)
class KindAvailability:
    """Availability of one interface type on a host."""

    status: Availability
    interfaces: tuple[InterfaceStatus, ...]


def combined_status(interfaces: tuple[InterfaceStatus, ...]) -> Availability:
    """Combine interface statuses like ``getInterfaceAvailabilityStatus()``."""
    relevant = [
        interface for interface in interfaces if interface.has_enabled_items
    ] or list(interfaces)
    statuses = {interface.available for interface in relevant}
    if Availability.MIXED in statuses:
        return Availability.MIXED
    if Availability.UNAVAILABLE in statuses:
        if statuses & {Availability.UNKNOWN, Availability.AVAILABLE}:
            return Availability.MIXED
        return Availability.UNAVAILABLE
    if Availability.UNKNOWN in statuses:
        return Availability.UNKNOWN
    return Availability.AVAILABLE


def host_availability(
    host: Host,
    interface_item_counts: Mapping[str, int],
    active_item_count: int,
) -> dict[AvailabilityKind, KindAvailability]:
    """Return the availability per interface type for a host.

    ``interface_item_counts`` maps interface ids to their number of enabled
    interface-bound items; ``active_item_count`` is the host's number of enabled
    active agent items.
    """
    grouped: dict[AvailabilityKind, list[InterfaceStatus]] = {}
    # The frontend lists the main interface first.
    for interface in sorted(host.interfaces, key=lambda item: not item.main):
        grouped.setdefault(_KIND_BY_TYPE[interface.type], []).append(
            InterfaceStatus(
                address=interface.address,
                available=interface.available,
                error=interface.error,
                has_enabled_items=interface_item_counts.get(interface.interface_id, 0)
                > 0,
            )
        )
    if active_item_count > 0:
        grouped.setdefault(AvailabilityKind.AGENT, []).append(
            InterfaceStatus(
                address=ACTIVE_CHECKS,
                available=host.active_available,
                error="",
                has_enabled_items=True,
            )
        )
    result: dict[AvailabilityKind, KindAvailability] = {}
    for kind in AvailabilityKind:
        if interfaces := tuple(grouped.get(kind, ())):
            result[kind] = KindAvailability(combined_status(interfaces), interfaces)
    return result
