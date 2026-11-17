"""Rule model: what ``rules.toml`` may say, validated with msgspec.

A rule's schema is deliberately small (see docs/BIBLE.md, "Alerting"). Only ``id``, ``sensor``,
``when``, ``value`` and ``severity`` are required; everything else has a default.
"""

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

import msgspec

from sensitivebox.alerts import globs
from sensitivebox.hwmon import Limits

type Severity = Literal["info", "warning", "critical"]
type When = Literal["above", "below", "equals"]
type Mode = Literal["sustained", "any"]
type OnMissing = Literal["ignore", "alert", "close"]

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|min|h)?\s*$")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, None: 1.0, "m": 60.0, "min": 60.0, "h": 3600.0}
_RELATIVE_RE = re.compile(r"^\s*(min|max|crit)\s*(?:([+-])\s*(\d+(?:\.\d+)?))?\s*$")


@dataclass(frozen=True, slots=True)
class Missing:
    """No reading this poll, and why (``ENODATA``, ``signal gone``, …). Never a number."""

    reason: str


type Value = float | str | Missing


class State(StrEnum):
    WARMING = "warming"
    OK = "ok"
    PENDING = "pending"
    FIRING = "firing"
    RESOLVED = "resolved"
    UNKNOWN = "unknown"


def parse_duration(text: str) -> float:
    """``"10s"``, ``"5m"``, ``"1h"``, ``"500ms"`` or a bare number of seconds."""
    m = _DURATION_RE.match(text)
    if not m:
        raise ValueError(f"not a duration: {text!r} (use e.g. 10s, 5m, 1h)")
    seconds = float(m[1]) * _UNIT_SECONDS[m[2]]
    if not math.isfinite(seconds):
        raise ValueError(f"duration too long: {text[:20]!r}…")
    return seconds


def _finite(number: float, what: str) -> float:
    if not math.isfinite(number):
        raise ValueError(f"{what} must be a finite number, not {number}")
    return number


@dataclass(frozen=True, slots=True)
class Threshold:
    """A fixed number, or a chip limit plus an offset (``crit-5``)."""

    fixed: float | None = None
    limit: Literal["min", "max", "crit"] | None = None
    offset: float = 0.0

    @classmethod
    def parse(cls, value: float | str) -> "Threshold":
        if not isinstance(value, str):
            return cls(fixed=_finite(float(value), "a threshold"))
        m = _RELATIVE_RE.match(value)
        if not m:
            raise ValueError(f"not a threshold: {value!r} (use a number, or e.g. crit-5, max-5)")
        sign = -1.0 if m[2] == "-" else 1.0
        limit: Literal["min", "max", "crit"] = m[1]  # pyright: ignore[reportAssignmentType]
        return cls(limit=limit, offset=sign * _finite(float(m[3] or 0), "an offset"))

    def resolve(self, limits: Limits | None) -> float | None:
        """The number to compare against, or ``None`` if the chip lacks that limit."""
        if self.fixed is not None:
            return self.fixed
        base = getattr(limits, self.limit or "", None) if limits is not None else None
        return None if base is None else float(base) + self.offset

    def describe(self) -> str:
        if self.fixed is not None:
            return f"{self.fixed:g}"
        sign = "-" if self.offset < 0 else "+"
        return f"{self.limit}{sign}{abs(self.offset):g}" if self.offset else str(self.limit)


_RECOVERY_SIDE = {"above": "below", "below": "above"}


def clear_on_breaching_side(when: When, value: float, clear: float) -> bool:
    """True when ``clear`` would count as recovered while ``value`` still breaches, so the
    alert could close and reopen at a constant reading (docs/BIBLE.md, "Boundaries")."""
    return clear > value if when == "above" else clear < value if when == "below" else False


def wrong_side_of(when: When, value: Threshold, clear: Threshold) -> bool:
    """The load-time check: decidable when both are fixed, or both relative to the same limit.
    A mix of the two is checked once the chip's limits are known."""
    if value.fixed is not None and clear.fixed is not None:
        return clear_on_breaching_side(when, value.fixed, clear.fixed)
    if value.limit is not None and value.limit == clear.limit:
        return clear_on_breaching_side(when, value.offset, clear.offset)
    return False


class OnlyIf(msgspec.Struct, forbid_unknown_fields=True, frozen=True):
    """Guard: the rule applies only while ``signal`` equals ``equals``. Named wildcards in
    ``signal`` (``{battery}``) take the value the rule's sensor glob bound them to, so the guard
    reads the same device as the sensor."""

    signal: str
    equals: float | str

    def __post_init__(self) -> None:
        if isinstance(self.equals, float):
            _finite(self.equals, "only_if.equals")


class Rule(msgspec.Struct, forbid_unknown_fields=True, frozen=True, kw_only=True):
    id: str
    sensor: str | tuple[str, ...]  # a TOML list becomes a tuple: rules are immutable (ADV-022)
    when: When
    value: float | str
    severity: Severity
    name: str = ""
    for_: str = msgspec.field(default="0s", name="for")
    mode: Mode = "sustained"
    clear: float | str | None = None
    clear_for: str | None = None
    on_missing: OnMissing = "ignore"
    missing_for: str = "30s"
    missing_severity: Severity = "info"
    cooldown: str = "5m"
    only_if: OnlyIf | None = None

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("id must not be empty")
        if not self.globs:
            raise ValueError("sensor must name at least one signal glob")
        if self.only_if is not None:
            for name in sorted(globs.names(self.only_if.signal)):
                for glob in self.globs:
                    if name not in globs.names(glob):
                        raise ValueError(
                            f"only_if uses {{{name}}}, which sensor {glob!r} doesn't bind"
                        )
        for duration in (self.for_, self.clear_for, self.missing_for, self.cooldown):
            if duration is not None:
                parse_duration(duration)
        if self.when == "equals" and isinstance(self.value, float):
            _finite(self.value, "value")
        if self.when != "equals":  # equals compares any value, e.g. "Discharging"
            value = Threshold.parse(self.value)
            clear = Threshold.parse(self.clear) if self.clear is not None else value
            if wrong_side_of(self.when, value, clear):
                raise ValueError(
                    f"clear {clear.describe()} is on the breaching side of {value.describe()}:"
                    f" a {self.when!r} rule must clear at or {_RECOVERY_SIDE[self.when]} its value"
                )

    def matches(self, signal: str) -> bool:
        return self.bindings(signal) is not None

    def bindings(self, signal: str) -> dict[str, str] | None:
        """The named wildcards bound by the first sensor glob ``signal`` matches, or ``None``
        when it matches none."""
        for glob in self.globs:
            bound = globs.match(glob, signal)
            if bound is not None:
                return bound
        return None

    @property
    def title(self) -> str:
        return self.name or self.id

    @property
    def globs(self) -> tuple[str, ...]:
        globs = [self.sensor] if isinstance(self.sensor, str) else self.sensor
        return tuple(g for g in globs if g.strip())

    @property
    def for_s(self) -> float:
        return parse_duration(self.for_)

    @property
    def clear_for_s(self) -> float:
        return parse_duration(self.clear_for) if self.clear_for is not None else self.for_s

    @property
    def missing_for_s(self) -> float:
        return parse_duration(self.missing_for)

    @property
    def cooldown_s(self) -> float:
        return parse_duration(self.cooldown)

    @property
    def threshold(self) -> Threshold:
        return Threshold.parse(self.value)

    @property
    def clear_threshold(self) -> Threshold:
        return Threshold.parse(self.clear) if self.clear is not None else self.threshold
