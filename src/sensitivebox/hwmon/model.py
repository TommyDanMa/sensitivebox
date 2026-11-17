"""Typed data model for hwmon chips, sensors and readings."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Kind(StrEnum):
    """What a sensor measures. Declaration order is the natural order within a chip."""

    TEMP = "temp"
    FAN = "fan"
    PWM = "pwm"
    IN = "in"
    CURR = "curr"
    POWER = "power"
    FREQ = "freq"


class Group(StrEnum):
    """Dashboard card a sensor belongs to. Declaration order is the dashboard order."""

    CPU = "CPU"
    MEMORY = "Memory"
    GPU = "GPU"
    STORAGE = "Storage"
    NETWORK = "Network"
    POWER = "Power"
    COOLING = "Cooling"
    SYSTEM = "System"


class ReadStatus(StrEnum):
    OK = "ok"
    UNAVAILABLE = "unavailable"  # read error (e.g. -ENODATA) or non-numeric ("N/A"); hidden in UI
    JUNK = "junk"  # physically impossible value; dropped


@dataclass(frozen=True, slots=True)
class Limits:
    """The chip's own limits, in the sensor's SI unit. ``None`` when absent or junk."""

    min: float | None = None
    max: float | None = None
    crit: float | None = None
    alarm: bool | None = None


@dataclass(frozen=True, slots=True)
class Sensor:
    id: str  # "<chip>/<bus id>/<channel>", never hwmonN
    chip: str
    bus_id: str
    channel: str  # e.g. "temp1", "pwm2"
    kind: Kind
    label: str
    group: Group
    unit: str
    input_path: Path
    limits: Limits = Limits()
    dropped_limits: tuple[tuple[str, float], ...] = ()  # junk limits, (name, SI value)


@dataclass(frozen=True, slots=True)
class Chip:
    name: str
    bus_id: str
    path: Path
    sensors: tuple[Sensor, ...]


@dataclass(frozen=True, slots=True)
class Reading:
    sensor_id: str
    value: float | None  # SI; None unless status is OK
    status: ReadStatus
    raw: int | None = None  # raw sysfs integer, kept for diagnostics
    reason: str | None = None  # why it is unavailable, e.g. "ENODATA", "not a number: 'N/A'"
