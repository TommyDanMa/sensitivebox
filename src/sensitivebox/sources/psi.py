"""Pressure stall information from ``/proc/pressure/{memory,cpu,io}``.

PSI is the share of time tasks were stalled waiting on a resource. ``some``: at least one task
waited; ``full``: every non-idle task waited at once. The ``total`` counters (microseconds) are
what Insights stores per interval; the averages are for the live view and alerts.
"""

from dataclasses import dataclass
from pathlib import Path

from sensitivebox.hwmon.reader import Opener, default_opener, read_attr

RESOURCES = ("memory", "cpu", "io")
RISING_MIN = 1.0  # %: below this, avg10 > avg60 is noise, not a trend


@dataclass(frozen=True, slots=True)
class PsiLine:
    avg10: float  # percent of the last 10 s
    avg60: float
    avg300: float
    total_us: int  # cumulative stall time since boot

    @property
    def rising(self) -> bool:
        return self.avg10 > self.avg60 and self.avg10 >= RISING_MIN


@dataclass(frozen=True, slots=True)
class Pressure:
    resource: str
    some: PsiLine
    full: PsiLine | None  # absent on older kernels for cpu


def _parse_line(rest: str) -> PsiLine | None:
    values: dict[str, str] = {}
    for item in rest.split():
        key, sep, value = item.partition("=")
        if sep:
            values[key] = value
    try:
        return PsiLine(
            avg10=float(values["avg10"]),
            avg60=float(values["avg60"]),
            avg300=float(values["avg300"]),
            total_us=int(values["total"]),
        )
    except (KeyError, ValueError):
        return None


def parse_pressure(resource: str, text: str) -> Pressure | None:
    lines: dict[str, PsiLine] = {}
    for line in text.splitlines():
        kind, _, rest = line.partition(" ")
        if kind in ("some", "full") and (parsed := _parse_line(rest)):
            lines[kind] = parsed
    if "some" not in lines:
        return None
    return Pressure(resource, lines["some"], lines.get("full"))


def read_pressure(
    proc_root: Path, resource: str, opener: Opener = default_opener
) -> Pressure | None:
    """``None`` when PSI is off (``psi=0``, or EOPNOTSUPP) or the file can't be parsed."""
    text = read_attr(proc_root / "pressure" / resource, opener)
    return parse_pressure(resource, text) if text else None


def read_all_pressure(proc_root: Path, opener: Opener = default_opener) -> dict[str, Pressure]:
    """Every resource that has PSI on this box, in ``RESOURCES`` order."""
    out: dict[str, Pressure] = {}
    for resource in RESOURCES:
        if (pressure := read_pressure(proc_root, resource, opener)) is not None:
            out[resource] = pressure
    return out
