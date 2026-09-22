"""Tests for availability, which must match the Zabbix frontend."""

import pytest

from custom_components.zabbix.api import (
    Availability,
    Host,
    HostInterface,
    InterfaceType,
)
from custom_components.zabbix.availability import (
    ACTIVE_CHECKS,
    AvailabilityKind,
    InterfaceStatus,
    combined_status,
    host_availability,
)

U, A, N, M = (
    Availability.UNKNOWN,
    Availability.AVAILABLE,
    Availability.UNAVAILABLE,
    Availability.MIXED,
)


def _status(available: Availability, *, enabled: bool = True) -> InterfaceStatus:
    return InterfaceStatus("x", available, "", has_enabled_items=enabled)


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([A], A),
        ([A, A], A),
        ([N], N),
        ([N, N], N),
        ([U], U),
        ([A, U], U),
        ([N, A], M),
        ([N, U], M),
        ([M, A], M),
    ],
)
def test_combined_status(statuses: list[Availability], expected: Availability) -> None:
    assert combined_status(tuple(_status(status) for status in statuses)) == expected


def test_interfaces_without_items_are_ignored_when_others_have_items() -> None:
    interfaces = (_status(N, enabled=False), _status(A))
    assert combined_status(interfaces) is A
    # ...but used when no interface has enabled items.
    assert combined_status((_status(N, enabled=False), _status(A, enabled=False))) is M


def _interface(
    interface_id: str, type_: InterfaceType, available: Availability, *, main: bool
) -> HostInterface:
    return HostInterface(
        interface_id=interface_id,
        type=type_,
        main=main,
        use_ip=True,
        ip="192.0.2.1",
        dns="",
        port="10050",
        available=available,
        error="",
    )


def _host(*interfaces: HostInterface, active: Availability = U) -> Host:
    return Host(
        host_id="1",
        host="h",
        name="H",
        description="",
        in_maintenance=False,
        maintenance_id="0",
        active_available=active,
        interfaces=interfaces,
        group_ids=frozenset(),
    )


def test_host_availability_groups_by_type_main_first() -> None:
    host = _host(
        _interface("2", InterfaceType.AGENT, N, main=False),
        _interface("1", InterfaceType.AGENT, A, main=True),
        _interface("3", InterfaceType.SNMP, U, main=True),
        _interface("4", InterfaceType.JMX, A, main=True),
    )
    result = host_availability(host, {"1": 3, "2": 1}, 0)
    assert set(result) == {
        AvailabilityKind.AGENT,
        AvailabilityKind.SNMP,
        AvailabilityKind.JMX,
    }
    agent = result[AvailabilityKind.AGENT]
    assert agent.status is M
    assert [interface.available for interface in agent.interfaces] == [A, N]
    assert result[AvailabilityKind.SNMP].status is U


def test_active_checks_are_part_of_agent_availability() -> None:
    host = _host(_interface("1", InterfaceType.AGENT, A, main=True), active=N)
    result = host_availability(host, {"1": 2}, 5)
    agent = result[AvailabilityKind.AGENT]
    assert agent.status is M
    assert agent.interfaces[-1].address == ACTIVE_CHECKS


def test_active_only_host() -> None:
    result = host_availability(_host(active=A), {}, 1)
    assert result[AvailabilityKind.AGENT].status is A


def test_host_without_interfaces_or_active_items() -> None:
    assert host_availability(_host(active=A), {}, 0) == {}
