"""Decide which files are sensors, what they measure, and which dashboard group they go in."""

import re
from collections.abc import Iterable
from fnmatch import fnmatchcase
from typing import NamedTuple

from sensitivebox.hwmon.model import Group, Kind

_INPUT_RE = re.compile(r"^(temp|fan|in|curr|power|freq)(\d+)_input$")
_PWM_RE = re.compile(r"^pwm(\d+)$")
_POWER_AVERAGE_RE = re.compile(r"^power(\d+)_average$")


class Attr(NamedTuple):
    kind: Kind
    channel: str
    fallback: bool  # only used when the channel has no primary input file


def parse_attr(name: str) -> Attr | None:
    """Classify one hwmon file name; ``None`` for anything that isn't a sensor value."""
    if m := _INPUT_RE.match(name):
        return Attr(Kind(m[1]), f"{m[1]}{m[2]}", fallback=False)
    if m := _PWM_RE.match(name):
        return Attr(Kind.PWM, f"pwm{m[1]}", fallback=False)
    if m := _POWER_AVERAGE_RE.match(name):
        # Older amdgpu kernels expose only power1_average.
        return Attr(Kind.POWER, f"power{m[1]}", fallback=True)
    return None


_KIND_ORDER = {kind: i for i, kind in enumerate(Kind)}


def _channel_number(channel: str) -> int:
    return int(re.sub(r"\D", "", channel))


def select_inputs(names: Iterable[str]) -> list[tuple[Kind, str, str]]:
    """Pick one value file per channel → ``(kind, channel, file name)`` in natural order."""
    chosen: dict[str, tuple[Attr, str]] = {}
    for name in names:
        attr = parse_attr(name)
        if attr is None:
            continue
        current = chosen.get(attr.channel)
        if current is None or (current[0].fallback and not attr.fallback):
            chosen[attr.channel] = (attr, name)
    ordered = sorted(
        chosen.values(),
        key=lambda item: (_KIND_ORDER[item[0].kind], _channel_number(item[0].channel)),
    )
    return [(attr.kind, attr.channel, name) for attr, name in ordered]


# Ordered: first match wins. Patterns are case-sensitive fnmatch globs on the chip name.
_GROUP_PATTERNS: tuple[tuple[Group, tuple[str, ...]], ...] = (
    (Group.CPU, ("k10temp", "coretemp", "zenpower", "cpu_thermal")),
    (Group.GPU, ("amdgpu", "radeon", "nouveau", "i915", "xe")),
    (Group.STORAGE, ("nvme", "drivetemp")),
    (Group.MEMORY, ("spd5118", "jc42")),
    (Group.POWER, ("BAT*", "AC", "ACAD", "AC[0-9]*", "ADP*", "ucsi_source_psy_*", "*_battery*")),
    (Group.NETWORK, ("mt7*", "iwlwifi*", "ath*", "rtw*", "*_phy[0-9]*")),
)


def group_for(chip: str, kind: Kind) -> Group:
    for group, patterns in _GROUP_PATTERNS:
        if any(fnmatchcase(chip, pattern) for pattern in patterns):
            return group
    if kind in (Kind.FAN, Kind.PWM):
        return Group.COOLING
    return Group.SYSTEM
