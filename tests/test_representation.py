"""Tests for mapping Zabbix item metadata to Home Assistant sensors."""

from datetime import UTC, datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
import pytest

from custom_components.zabbix.api import Item, ItemValue, ValueMap, ValueType
from custom_components.zabbix.representation import (
    ValueKind,
    convert,
    representation_for,
)


def _item(
    value_type: ValueType = ValueType.FLOAT,
    units: str = "",
    value_map: ValueMap | None = None,
) -> Item:
    return Item(
        item_id="1",
        host_id="1",
        key="k",
        name="n",
        type=0,
        value_type=value_type,
        units=units,
        value_map=value_map,
        master_item_id="0",
    )


def _value(raw: str, clock: int = 1_000_000) -> ItemValue:
    return ItemValue(
        item_id="1", last_value=raw, last_clock=clock, supported=True, error=""
    )


@pytest.mark.parametrize(
    ("units", "device_class", "unit"),
    [
        ("%", None, "%"),
        ("B", SensorDeviceClass.DATA_SIZE, "B"),
        ("GiB", SensorDeviceClass.DATA_SIZE, "GiB"),
        ("bps", SensorDeviceClass.DATA_RATE, "bit/s"),
        ("Bps", SensorDeviceClass.DATA_RATE, "B/s"),
        ("s", SensorDeviceClass.DURATION, "s"),
        ("!ms", SensorDeviceClass.DURATION, "ms"),
        ("C", SensorDeviceClass.TEMPERATURE, "°C"),
        ("°F", SensorDeviceClass.TEMPERATURE, "°F"),
        ("W", SensorDeviceClass.POWER, "W"),
        ("dBm", SensorDeviceClass.SIGNAL_STRENGTH, "dBm"),
        ("!r/s", None, "r/s"),
        ("IOPS", None, "IOPS"),
        ("", None, None),
        ("{#UNITS}", None, None),
        ("{$UNIT}", None, None),
        ("  ", None, None),
    ],
)
def test_numeric_units(
    units: str, device_class: SensorDeviceClass | None, unit: str | None
) -> None:
    rep = representation_for(_item(units=units))
    assert rep.kind is ValueKind.NUMBER
    assert rep.device_class == device_class
    assert rep.unit == unit
    assert rep.state_class is SensorStateClass.MEASUREMENT


def test_number_conversion() -> None:
    rep = representation_for(_item(ValueType.UNSIGNED))
    assert convert(rep, _item(ValueType.UNSIGNED), _value("42")).state == 42
    assert convert(rep, _item(ValueType.UNSIGNED), _value("4.5")).state == 4.5
    assert convert(rep, _item(ValueType.UNSIGNED), _value("x")).state is None
    assert convert(rep, _item(), _value("1.25")).state == 1.25
    assert convert(rep, _item(), _value("nope")).state is None
    assert convert(rep, _item(), _value("1", clock=0)).state is None


def test_value_map_becomes_enum() -> None:
    value_map = ValueMap.from_api(
        {
            "valuemapid": "1",
            "name": "Service state",
            "mappings": [
                {"type": "0", "value": "0", "newvalue": "Down"},
                {"type": "0", "value": "1", "newvalue": "Up"},
            ],
        }
    )
    item = _item(ValueType.UNSIGNED, units="%", value_map=value_map)
    rep = representation_for(item)
    assert rep.kind is ValueKind.ENUM
    assert rep.device_class is SensorDeviceClass.ENUM
    assert rep.options == ("Down", "Up")
    assert rep.unit is None
    assert convert(rep, item, _value("1")).state == "Up"
    unmapped = convert(rep, item, _value("7"))
    assert unmapped.state is None
    assert unmapped.raw == "7"


def test_empty_value_map_is_ignored() -> None:
    value_map = ValueMap.from_api({"valuemapid": "1", "name": "x", "mappings": []})
    assert representation_for(_item(value_map=value_map)).kind is ValueKind.NUMBER


@pytest.mark.parametrize("value_type", [ValueType.CHAR, ValueType.TEXT, ValueType.LOG])
def test_text(value_type: ValueType) -> None:
    item = _item(value_type, units="B")
    rep = representation_for(item)
    assert rep.kind is ValueKind.TEXT
    assert rep.unit is None
    assert rep.state_class is None
    assert convert(rep, item, _value("hello")).state == "hello"
    long = convert(rep, item, _value("y" * 300))
    assert long.state == "y" * 255
    assert long.raw == "y" * 300


def test_unixtime() -> None:
    item = _item(ValueType.UNSIGNED, units="unixtime")
    rep = representation_for(item)
    assert rep.kind is ValueKind.TIMESTAMP
    assert rep.device_class is SensorDeviceClass.TIMESTAMP
    assert convert(rep, item, _value("1700000000")).state == datetime(
        2023, 11, 14, 22, 13, 20, tzinfo=UTC
    )
    assert convert(rep, item, _value("0")).state is None
    assert convert(rep, item, _value("99999999999999999")).state is None


def test_uptime_becomes_boot_time() -> None:
    item = _item(ValueType.UNSIGNED, units="uptime")
    rep = representation_for(item)
    assert rep.kind is ValueKind.BOOT_TIME
    assert convert(rep, item, _value("100", clock=1_700_000_100)).state == datetime(
        2023, 11, 14, 22, 13, 20, tzinfo=UTC
    )
    assert convert(rep, item, _value("abc")).state is None
