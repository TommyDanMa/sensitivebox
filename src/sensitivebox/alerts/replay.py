"""Replay recorded input events through the real engine.

There is one evaluation path. ``sbox rule <id>`` and the rule editor's preview replay the input
events the store recorded (``Store.inputs``) with this function instead of re-implementing the
rules, so "why didn't it fire?" is always answered by the code that decides whether it fires.
The events carry everything the live engine saw, rule and limit changes included (ADV-005).
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import msgspec

from sensitivebox.alerts.engine import Engine, Evaluation, ManualClock, Step, Transition
from sensitivebox.alerts.events import (
    Checkpoint,
    EngineSettings,
    InputEvent,
    LimitsChanged,
    OpenAlerts,
    Restart,
    Resume,
    RulesLoaded,
    Suspend,
    Tick,
)
from sensitivebox.alerts.model import Rule
from sensitivebox.hwmon import Limits

type Event = InputEvent

__all__ = [
    "EngineSettings",
    "Event",
    "LimitsChanged",
    "ReplayResult",
    "Resume",
    "RulesLoaded",
    "Suspend",
    "Tick",
    "replay",
]


@dataclass(frozen=True, slots=True)
class ReplayResult:
    evaluations: tuple[Evaluation, ...]
    transitions: tuple[Transition, ...]


def replay(
    rules: Sequence[Rule],
    events: Iterable[InputEvent],
    limits: Mapping[str, Limits] | None = None,
) -> ReplayResult:
    """Run ``events`` (in the order recorded) through a fresh engine and collect what it did.

    ``rules`` and ``limits`` are the starting configuration; ``RulesLoaded`` and ``LimitsChanged``
    events change it exactly as ``Engine.load`` and ``Engine.set_limits`` did live.
    """
    runner = ReplayRunner(rules, limits)
    evaluations: list[Evaluation] = []
    transitions: list[Transition] = []
    for event in events:
        step = runner.feed(event)
        evaluations.extend(step.evaluations)
        transitions.extend(step.transitions)
    return ReplayResult(tuple(evaluations), tuple(transitions))


class ReplayRunner:
    """Incremental replay also advances the single ring-boundary checkpoint."""

    def __init__(self, rules: Sequence[Rule] = (), limits: Mapping[str, Limits] | None = None):
        self.clock = ManualClock()
        self.engine = Engine(rules, clock=self.clock, limits=limits)

    def feed(self, event: InputEvent) -> Step:
        self.clock.now = event.at
        match event:
            case Restart():
                self.engine = Engine((), clock=self.clock)
            case Checkpoint(payload=payload):
                self.engine.restore(payload)
            case Tick(signals=signals):
                return self.engine.evaluate(signals)
            case OpenAlerts(payload=payload):
                self.engine.restore_alerts(msgspec.json.decode(payload, type=list[Transition]))
            case Suspend():
                self.engine.suspend()
            case Resume():
                self.engine.resume()
            case LimitsChanged(limits=changed):
                self.engine.set_limits(changed)
            case RulesLoaded(rules=loaded):
                return Step(transitions=self.engine.load(loaded))
            case EngineSettings(max_gap=max_gap):
                self.engine.configure(max_gap)
        return Step()
