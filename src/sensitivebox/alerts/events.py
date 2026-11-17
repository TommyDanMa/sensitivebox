"""Input events: everything the alert engine sees, in the order it sees it.

What's recorded is what gets replayed (docs/BIBLE.md, "Alerting"; review ADV-005). The engine
reports each of these to its ``journal`` as it happens, the store keeps them in the 1 h raw ring,
and :func:`sensitivebox.alerts.replay.replay` feeds them back through a fresh engine, so a replay
gives exactly what the live daemon did. ``at`` is the engine's (monotonic) clock.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from sensitivebox.alerts.model import Rule, Value
from sensitivebox.hwmon import Limits


@dataclass(frozen=True, slots=True)
class Tick:
    """One poll: every signal's value or MISSING, guard signals included."""

    at: float
    signals: Mapping[str, Value]


@dataclass(frozen=True, slots=True)
class Suspend:
    at: float


@dataclass(frozen=True, slots=True)
class Resume:
    at: float


@dataclass(frozen=True, slots=True)
class LimitsChanged:
    """The chip limits relative thresholds resolve against (at start, and after rediscovery)."""

    at: float
    limits: Mapping[str, Limits]


@dataclass(frozen=True, slots=True)
class RulesLoaded:
    """The rule set in force (at start, and whenever rules are loaded or edited)."""

    at: float
    rules: tuple[Rule, ...]


@dataclass(frozen=True, slots=True)
class EngineSettings:
    """How the engine treats time: a gap between real readings longer than ``max_gap`` seconds
    is MISSING for that span (``None``: no limit)."""

    at: float
    max_gap: float | None


@dataclass(frozen=True, slots=True)
class Restart:
    at: float


@dataclass(frozen=True, slots=True)
class OpenAlerts:
    at: float
    payload: str


@dataclass(frozen=True, slots=True)
class Checkpoint:
    at: float
    payload: str


type InputEvent = (
    OpenAlerts
    | Restart
    | Checkpoint
    | Tick
    | Suspend
    | Resume
    | LimitsChanged
    | RulesLoaded
    | EngineSettings
)
type Journal = Callable[[InputEvent], None]
