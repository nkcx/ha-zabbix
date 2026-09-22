"""How a Zabbix item is represented as a Home Assistant sensor.

Nothing here reinterprets data: an item is shown using its own Zabbix metadata
(value type, units and value map), translated to the matching HA concepts.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfDataRate,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfFrequency,
    UnitOfInformation,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)

from .api import Item, ItemValue, ValueType
from .const import MAX_STATE_LENGTH


class ValueKind(StrEnum):
    """How the raw value is converted."""

    NUMBER = "number"
    ENUM = "enum"
    TIMESTAMP = "timestamp"
    BOOT_TIME = "boot_time"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class Representation:
    """Sensor settings derived from item metadata."""

    kind: ValueKind
    device_class: SensorDeviceClass | None = None
    unit: str | None = None
    state_class: SensorStateClass | None = None
    options: tuple[str, ...] | None = None


_M = SensorStateClass.MEASUREMENT

# Zabbix unit -> (device class, HA unit)
_UNITS: dict[str, tuple[SensorDeviceClass | None, str]] = {
    "%": (None, PERCENTAGE),
    "B": (SensorDeviceClass.DATA_SIZE, UnitOfInformation.BYTES),
    "KB": (SensorDeviceClass.DATA_SIZE, UnitOfInformation.KILOBYTES),
    "kB": (SensorDeviceClass.DATA_SIZE, UnitOfInformation.KILOBYTES),
    "MB": (SensorDeviceClass.DATA_SIZE, UnitOfInformation.MEGABYTES),
    "GB": (SensorDeviceClass.DATA_SIZE, UnitOfInformation.GIGABYTES),
    "TB": (SensorDeviceClass.DATA_SIZE, UnitOfInformation.TERABYTES),
    "KiB": (SensorDeviceClass.DATA_SIZE, UnitOfInformation.KIBIBYTES),
    "MiB": (SensorDeviceClass.DATA_SIZE, UnitOfInformation.MEBIBYTES),
    "GiB": (SensorDeviceClass.DATA_SIZE, UnitOfInformation.GIBIBYTES),
    "TiB": (SensorDeviceClass.DATA_SIZE, UnitOfInformation.TEBIBYTES),
    "bps": (SensorDeviceClass.DATA_RATE, UnitOfDataRate.BITS_PER_SECOND),
    "Kbps": (SensorDeviceClass.DATA_RATE, UnitOfDataRate.KILOBITS_PER_SECOND),
    "Mbps": (SensorDeviceClass.DATA_RATE, UnitOfDataRate.MEGABITS_PER_SECOND),
    "Gbps": (SensorDeviceClass.DATA_RATE, UnitOfDataRate.GIGABITS_PER_SECOND),
    "Bps": (SensorDeviceClass.DATA_RATE, UnitOfDataRate.BYTES_PER_SECOND),
    "KBps": (SensorDeviceClass.DATA_RATE, UnitOfDataRate.KILOBYTES_PER_SECOND),
    "MBps": (SensorDeviceClass.DATA_RATE, UnitOfDataRate.MEGABYTES_PER_SECOND),
    "GBps": (SensorDeviceClass.DATA_RATE, UnitOfDataRate.GIGABYTES_PER_SECOND),
    "B/s": (SensorDeviceClass.DATA_RATE, UnitOfDataRate.BYTES_PER_SECOND),
    "MB/s": (SensorDeviceClass.DATA_RATE, UnitOfDataRate.MEGABYTES_PER_SECOND),
    "s": (SensorDeviceClass.DURATION, UnitOfTime.SECONDS),
    "ms": (SensorDeviceClass.DURATION, UnitOfTime.MILLISECONDS),
    "C": (SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS),
    "°C": (SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS),
    "F": (SensorDeviceClass.TEMPERATURE, UnitOfTemperature.FAHRENHEIT),
    "°F": (SensorDeviceClass.TEMPERATURE, UnitOfTemperature.FAHRENHEIT),
    "W": (SensorDeviceClass.POWER, UnitOfPower.WATT),
    "kW": (SensorDeviceClass.POWER, UnitOfPower.KILO_WATT),
    "V": (SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT),
    "mV": (SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.MILLIVOLT),
    "A": (SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE),
    "mA": (SensorDeviceClass.CURRENT, UnitOfElectricCurrent.MILLIAMPERE),
    "Hz": (SensorDeviceClass.FREQUENCY, UnitOfFrequency.HERTZ),
    "kHz": (SensorDeviceClass.FREQUENCY, UnitOfFrequency.KILOHERTZ),
    "MHz": (SensorDeviceClass.FREQUENCY, UnitOfFrequency.MEGAHERTZ),
    "GHz": (SensorDeviceClass.FREQUENCY, UnitOfFrequency.GIGAHERTZ),
    "dBm": (SensorDeviceClass.SIGNAL_STRENGTH, SIGNAL_STRENGTH_DECIBELS_MILLIWATT),
    "dB": (SensorDeviceClass.SIGNAL_STRENGTH, SIGNAL_STRENGTH_DECIBELS),
}


def _clean_unit(units: str) -> str | None:
    """Strip Zabbix's no-conversion marker and drop unresolved macros."""
    unit = units.strip().removeprefix("!").strip()
    if not unit or "{#" in unit or "{$" in unit:
        return None
    return unit


def representation_for(item: Item) -> Representation:
    """Return how an item is shown in Home Assistant."""
    if item.value_map is not None and item.value_map.options:
        return Representation(
            ValueKind.ENUM,
            device_class=SensorDeviceClass.ENUM,
            options=tuple(item.value_map.options),
        )
    if not item.value_type.is_numeric:
        return Representation(ValueKind.TEXT)
    unit = _clean_unit(item.units)
    if unit == "unixtime":
        return Representation(
            ValueKind.TIMESTAMP, device_class=SensorDeviceClass.TIMESTAMP
        )
    if unit == "uptime":
        return Representation(
            ValueKind.BOOT_TIME, device_class=SensorDeviceClass.TIMESTAMP
        )
    if unit is None:
        return Representation(ValueKind.NUMBER, state_class=_M)
    if unit in _UNITS:
        device_class, ha_unit = _UNITS[unit]
        return Representation(
            ValueKind.NUMBER, device_class=device_class, unit=ha_unit, state_class=_M
        )
    return Representation(ValueKind.NUMBER, unit=unit, state_class=_M)


def _number(raw: str, value_type: ValueType) -> float | int | None:
    try:
        if value_type is ValueType.UNSIGNED:
            return int(raw)
        return float(raw)
    except ValueError:
        try:
            return float(raw)
        except ValueError:
            return None


def _timestamp(seconds: float | int | None) -> datetime | None:
    if seconds is None or seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except OverflowError, OSError, ValueError:
        return None


@dataclass(frozen=True, slots=True)
class ConvertedValue:
    """A converted value plus the raw value when it could not be shown."""

    state: str | float | int | datetime | None
    raw: str | None = None


def convert(rep: Representation, item: Item, value: ItemValue) -> ConvertedValue:
    """Convert an item's latest value for its representation."""
    if not value.has_value:
        return ConvertedValue(None)
    raw = value.last_value
    match rep.kind:
        case ValueKind.ENUM:
            assert item.value_map is not None
            mapped = item.value_map.map(raw)
            return ConvertedValue(mapped, raw=None if mapped is not None else raw)
        case ValueKind.TEXT:
            if len(raw) > MAX_STATE_LENGTH:
                return ConvertedValue(raw[:MAX_STATE_LENGTH], raw=raw)
            return ConvertedValue(raw)
        case ValueKind.TIMESTAMP:
            return ConvertedValue(_timestamp(_number(raw, item.value_type)))
        case ValueKind.BOOT_TIME:
            uptime = _number(raw, item.value_type)
            if uptime is None:
                return ConvertedValue(None)
            return ConvertedValue(_timestamp(value.last_clock - uptime))
        case _:
            return ConvertedValue(_number(raw, item.value_type))
