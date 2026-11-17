"""Convert raw sysfs integers to SI floats and spot physically impossible values."""

from sensitivebox.hwmon.model import Kind

# sysfs units (Documentation/hwmon/sysfs-interface.rst) → divisor to reach the display unit.
_DIVISOR: dict[Kind, float] = {
    Kind.TEMP: 1000.0,  # millidegree Celsius
    Kind.IN: 1000.0,  # millivolt
    Kind.CURR: 1000.0,  # milliampere
    Kind.POWER: 1_000_000.0,  # microwatt
    Kind.FREQ: 1.0,  # Hz
    Kind.FAN: 1.0,  # RPM
    Kind.PWM: 255 / 100,  # 0-255 duty cycle → percent
}

UNIT: dict[Kind, str] = {
    Kind.TEMP: "°C",
    Kind.IN: "V",
    Kind.CURR: "A",
    Kind.POWER: "W",
    Kind.FREQ: "Hz",
    Kind.FAN: "RPM",
    Kind.PWM: "%",
}

# Anything at or below absolute zero, or hotter than this, is a firmware placeholder
# (e.g. NVMe "Sensor 1" reporting a 65 261.85 °C high limit).
TEMP_MIN_C = -273.0
TEMP_MAX_C = 65_000.0


def to_si(kind: Kind, raw: int) -> float:
    return raw / _DIVISOR[kind]


def is_junk(kind: Kind, value: float) -> bool:
    """True for values no real sensor can report. Zero is always a valid reading."""
    if kind is Kind.TEMP:
        return value <= TEMP_MIN_C or value > TEMP_MAX_C
    if kind is Kind.FAN:
        return value < 0
    return False
