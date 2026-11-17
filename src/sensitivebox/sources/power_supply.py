"""Batteries, mains adapters and USB-C source ports from ``/sys/class/power_supply``.

Directory names are what the kernel calls the supply (``BAT1``, ``ACAD``,
``ucsi-source-psy-USBC000:001``), and the daemon publishes them unchanged. Voltage and current
are in micro-units. A USB-C port with nothing plugged in reads 0 V / 0 A and ``online`` = 0;
that is "no source", not a 0 W charger, so ``power_w`` is ``None`` unless the port is online.
"""

from dataclasses import dataclass
from pathlib import Path

from sensitivebox.hwmon.reader import Opener, default_opener, read_attr, read_raw

_MICRO = 1_000_000.0


@dataclass(frozen=True, slots=True)
class PowerSupply:
    name: str
    kind: str | None  # the ``type`` file: "Battery", "Mains", "USB", …
    capacity: float | None  # percent, batteries only
    status: str | None  # "Charging", "Discharging", "Full", …
    online: bool | None  # mains and USB-C ports
    voltage_v: float | None
    current_a: float | None

    @property
    def power_w(self) -> float | None:
        """Negotiated power, only for an online supply that reports both voltage and current."""
        if not self.online or self.voltage_v is None or self.current_a is None:
            return None
        return self.voltage_v * self.current_a

    @property
    def is_usb_source(self) -> bool:
        return self.name.startswith("ucsi-source-psy-")


def _number(path: Path, opener: Opener) -> int | None:
    return read_raw(path, opener).value


def _scaled(path: Path, opener: Opener) -> float | None:
    raw = _number(path, opener)
    return None if raw is None else raw / _MICRO


def read_power_supply(directory: Path, opener: Opener = default_opener) -> PowerSupply:
    online = _number(directory / "online", opener)
    capacity = _number(directory / "capacity", opener)
    return PowerSupply(
        name=directory.name,
        kind=read_attr(directory / "type", opener),
        capacity=None if capacity is None else float(capacity),
        status=read_attr(directory / "status", opener),
        online=None if online is None else online != 0,
        voltage_v=_scaled(directory / "voltage_now", opener),
        current_a=_scaled(directory / "current_now", opener),
    )


def read_power_supplies(
    sysfs_root: Path, opener: Opener = default_opener
) -> tuple[PowerSupply, ...]:
    """Every supply under ``class/power_supply``, sorted by name. Empty when there are none."""
    try:
        entries = sorted((sysfs_root / "class" / "power_supply").iterdir())
    except OSError:
        return ()
    return tuple(read_power_supply(entry, opener) for entry in entries)
