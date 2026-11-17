"""The alert engine: one small state machine per (rule, signal), driven by an injected clock.

Contract (docs/BIBLE.md, "Alerting"):

- Input per poll is ``value | Missing(reason)``. Missing is never 0, never above or below a
  threshold, and never a recovery. It *pauses* the ``for`` and ``clear_for`` timers.
- States: Warming → OK → Pending → Firing → Resolved, plus Unknown (missing, no alert open).
- Loading, editing a rule and ``resume()`` put it in Warming: it can't fire on values until it
  has seen a full ``for`` window of real readings (at least one). An open alert survives that
  and is not notified again.
- ``suspend()`` (logind PrepareForSleep/PrepareForShutdown) stops evaluation and the
  missing-data timers, so sleep and shutdown never fire anything.
- After ``missing_for`` without data, ``on_missing`` decides: ignore, alert (at
  ``missing_severity``) or close ("closed: no data").

The engine never notifies anyone: it returns transitions, and the notification pipeline (mute,
cooldown, notifiers) runs after it. Everything it is given (rules, limits, each poll's readings,
suspend and resume) is reported to an optional ``journal``, so the store can record it and
``replay`` can reproduce exactly what the engine did.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

import msgspec

from sensitivebox.alerts import globs
from sensitivebox.alerts.events import (
    EngineSettings,
    InputEvent,
    Journal,
    LimitsChanged,
    OpenAlerts,
    Restart,
    Resume,
    RulesLoaded,
    Suspend,
    Tick,
)
from sensitivebox.alerts.model import (
    Missing,
    Rule,
    Severity,
    State,
    Value,
    clear_on_breaching_side,
)
from sensitivebox.hwmon import Limits

CLOSED_NO_DATA = "closed: no data"
RECOVERED = "recovered"
DATA_BACK = "data back"
RULE_REMOVED = "rule removed"
RULE_CHANGED = "rule no longer matches"
GUARD_FALSE = "not applicable (guard)"


class Clock(Protocol):
    """Seconds on a **monotonic** clock that stops during suspend (``time.monotonic`` on Linux).
    Every timer runs on it, never on the wall clock (docs/BIBLE.md, "Time"). A step backwards
    or a zero step counts as no time passing."""

    def __call__(self) -> float: ...


class ManualClock:
    """A clock that only moves when told to: for tests, replay and the simulator."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TransitionKind(StrEnum):
    FIRING = "firing"
    RESOLVED = "resolved"


@dataclass(frozen=True, slots=True)
class Transition:
    """An alert opening or closing: the only thing that can lead to a notification."""

    at: float
    rule_id: str
    signal: str
    kind: TransitionKind
    severity: Severity
    reason: str  # "95 above 90 for 10 s", "recovered", "closed: no data", …
    value: float | str | None
    threshold: float | None
    opened_at: float
    no_data: bool = False  # a missing-data alert (on_missing = "alert")


@dataclass(frozen=True, slots=True)
class Evaluation:
    """One rule evaluated against one signal at one poll. Stored in the 1 h ring (Prompt 3)
    so ``doctor --rule`` can answer "why didn't it fire?"."""

    at: float
    rule_id: str
    signal: str
    value: float | str | None
    missing: str | None  # the reason, when the reading was Missing
    state: State
    reason: str
    threshold: float | None
    alert_severity: Severity | None = None
    no_data: bool = False


@dataclass(frozen=True, slots=True)
class Step:
    evaluations: tuple[Evaluation, ...] = ()
    transitions: tuple[Transition, ...] = ()


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:g} s"
    total = round(seconds)
    if total < 3600:
        m, s = divmod(total, 60)
        return f"{m} min {s} s" if s else f"{m} min"
    h, rem = divmod(total, 3600)
    m = rem // 60
    return f"{h} h {m} min" if m else f"{h} h"


@dataclass(slots=True)
class _Alert:
    opened_at: float
    severity: Severity
    no_data: bool
    threshold: float | None


@dataclass(slots=True)
class _Instance:
    alert: _Alert | None = None
    state: State = State.WARMING
    warming: bool = True
    warm_elapsed: float = 0.0
    warm_count: int = 0
    in_breach: bool = False  # the last *real* reading breached
    breach_elapsed: float = 0.0
    in_clear: bool = False  # the last real reading was past `clear`
    clear_elapsed: float = 0.0
    last_t: float | None = None
    last_real: bool = False
    missing_since: float | None = None

    def restart(self) -> None:
        """Back to Warming with fresh timers; an open alert stays open."""
        alert = self.alert
        for name, default in _FRESH.items():
            setattr(self, name, default)
        self.alert = alert
        self.state = State.WARMING


_FRESH = {
    "warming": True,
    "warm_elapsed": 0.0,
    "warm_count": 0,
    "in_breach": False,
    "breach_elapsed": 0.0,
    "in_clear": False,
    "clear_elapsed": 0.0,
    "last_t": None,
    "last_real": False,
    "missing_since": None,
}


@dataclass(slots=True)
class _Ctx:
    """Everything one evaluation needs to know about its rule and signal."""

    rule: Rule
    signal: str
    threshold: float | None
    clear: float | None
    guard_false: bool = False
    unavailable: str | None = None  # why the thresholds can't be resolved, e.g. "no crit limit"
    out: list[Transition] = field(default_factory=lambda: [])


def _later(last: float | None, now: float) -> float:
    """The clock never runs backwards for an instance: a backwards step is no time passing."""
    return now if last is None else max(last, now)


def _matches(rule: Rule, signal: str) -> bool:
    return rule.matches(signal)


@dataclass(frozen=True, slots=True)
class EngineMemory:
    rules: tuple[Rule, ...]
    limits: dict[str, Limits]
    known: tuple[str, ...]
    instances: list[tuple[str, str, _Instance]]
    suspended: bool
    max_gap: float | None
    version: int = 1


class Engine:
    def __init__(
        self,
        rules: Sequence[Rule],
        *,
        clock: Clock,
        limits: Mapping[str, Limits] | None = None,
        journal: Journal | None = None,
        max_gap: float | None = None,
    ) -> None:
        """``max_gap``: a gap between real readings longer than this (the daemon passes 3x its
        poll interval) counts as MISSING for that span, never as breach time."""
        self._clock = clock
        self._journal = journal
        self._max_gap = max_gap
        self._limits: dict[str, Limits] = dict(limits or {})
        self._known: set[str] = set(self._limits)
        self._rules: dict[str, Rule] = {}
        self._instances: dict[tuple[str, str], _Instance] = {}
        self._suspended = False
        self._report(Restart(clock()))
        self._report(EngineSettings(clock(), max_gap))
        self._report(LimitsChanged(clock(), dict(self._limits)))
        self.load(rules)

    def checkpoint(self) -> str:
        return msgspec.json.encode(
            EngineMemory(
                self.rules,
                self._limits,
                tuple(sorted(self._known)),
                [(r, s, inst) for (r, s), inst in self._instances.items()],
                self._suspended,
                self._max_gap,
            )
        ).decode()

    def restore(self, payload: str) -> None:
        memory = msgspec.json.decode(payload, type=EngineMemory)
        if memory.version != 1:
            raise ValueError("unsupported engine checkpoint")
        self._rules = {r.id: r for r in memory.rules}
        self._limits = memory.limits
        self._known = set(memory.known)
        self._instances = {(r, s): inst for r, s, inst in memory.instances}
        self._suspended, self._max_gap = memory.suspended, memory.max_gap

    def restore_alerts(self, alerts: Sequence[Transition]) -> None:
        """Carry same-boot open incidents into a fresh Warming session."""
        self._report(OpenAlerts(self._clock(), msgspec.json.encode(list(alerts)).decode()))
        for t in alerts:
            rule = self._rules.get(t.rule_id)
            if rule is not None and rule.matches(t.signal):
                inst = _Instance()
                inst.alert = _Alert(t.opened_at, t.severity, t.no_data, t.threshold)
                self._instances[(t.rule_id, t.signal)] = inst
                self._known.add(t.signal)

    def _report(self, event: InputEvent) -> None:
        if self._journal is not None:
            self._journal(event)

    # -- configuration --------------------------------------------------------------------------

    def load(self, rules: Sequence[Rule]) -> tuple[Transition, ...]:
        """Replace the rule set. New and **edited** rules start in Warming; unchanged rules keep
        their state. Alerts of removed rules close with the reason "rule removed"."""
        self._report(RulesLoaded(self._clock(), tuple(rules)))
        new = {rule.id: rule for rule in rules}
        out: list[Transition] = []
        for (rule_id, signal), inst in list(self._instances.items()):
            old = self._rules[rule_id]
            if rule_id not in new:
                if inst.alert is not None:
                    out.append(self._close(old, signal, inst, RULE_REMOVED, None))
                del self._instances[(rule_id, signal)]
            elif new[rule_id] != old:
                inst.restart()
        self._rules = new
        return tuple(out)

    def set_limits(self, limits: Mapping[str, Limits]) -> None:
        """New chip limits after rediscovery (relative thresholds resolve against these)."""
        self._limits = dict(limits)
        self._known |= set(self._limits)
        self._report(LimitsChanged(self._clock(), dict(self._limits)))

    def configure(self, max_gap: float | None) -> None:
        """Change how time is treated (replay applies the recorded settings with this)."""
        self._max_gap = max_gap
        self._report(EngineSettings(self._clock(), max_gap))

    @property
    def rules(self) -> tuple[Rule, ...]:
        return tuple(self._rules.values())

    # -- sleep ----------------------------------------------------------------------------------

    @property
    def suspended(self) -> bool:
        return self._suspended

    def suspend(self) -> None:
        """PrepareForSleep(true) / PrepareForShutdown(true): stop evaluating entirely."""
        self._report(Suspend(self._clock()))
        self._suspended = True

    def resume(self) -> None:
        """PrepareForSleep(false): every rule warms up again, missing-data timers from zero."""
        self._report(Resume(self._clock()))
        self._suspended = False
        for inst in self._instances.values():
            inst.restart()

    # -- queries --------------------------------------------------------------------------------

    def state(self, rule_id: str, signal: str) -> State:
        inst = self._instances.get((rule_id, signal))
        return inst.state if inst is not None else State.UNKNOWN

    def states(self) -> dict[tuple[str, str], State]:
        return {key: inst.state for key, inst in self._instances.items()}

    def resolved_clear(self, rule_id: str, signal: str) -> float | None:
        """The effective clear for this rule/signal, including defaults and unavailable cases."""
        return self._context(self._rules[rule_id], signal).clear

    def unwatched(self, signals: Iterable[str]) -> list[str]:
        """Signals no rule watches: shown grey (Unknown), never green."""
        return sorted(s for s in signals if not any(_matches(r, s) for r in self._rules.values()))

    def problems(self) -> list[tuple[str, str, str]]:
        """``(rule id, signal, reason)`` for matching signals a rule can't apply to, e.g. a
        relative threshold on a chip without that limit."""
        out: list[tuple[str, str, str]] = []
        for rule in self._rules.values():
            for signal in sorted(self._known):
                if _matches(rule, signal):
                    why = self._context(rule, signal).unavailable
                    if why is not None:
                        out.append((rule.id, signal, why))
        return out

    # -- evaluation -----------------------------------------------------------------------------

    def evaluate(self, signals: Mapping[str, Value]) -> Step:
        """Evaluate every rule against this poll's readings. Does nothing while suspended."""
        if self._suspended:
            return Step()
        now = self._clock()
        self._report(Tick(now, dict(signals)))
        self._known |= signals.keys()
        evaluations: list[Evaluation] = []
        transitions: list[Transition] = []
        for rule in self._rules.values():
            current = {s for s in signals if _matches(rule, s)}
            tracked = {sig for (rid, sig) in self._instances if rid == rule.id}
            for signal in sorted(current | tracked):
                inst = self._instances.get((rule.id, signal))
                if signal not in current and signal in tracked and not _matches(rule, signal):
                    # The rule was edited and no longer matches this signal.
                    if inst is not None and inst.alert is not None:
                        transitions.append(self._close(rule, signal, inst, RULE_CHANGED, None))
                    del self._instances[(rule.id, signal)]
                    continue
                ctx = self._context(rule, signal)
                if inst is None:
                    inst = self._instances[(rule.id, signal)] = _Instance()
                reading = self._reading(rule, signal, signals, ctx)
                if ctx.unavailable is not None and not ctx.guard_false:
                    # A threshold that can't be resolved is no reading, never a skipped poll:
                    # it is recorded, pauses the timers and applies on_missing (ADV-006).
                    reading = Missing(ctx.unavailable)
                evaluations.append(self._step(ctx, inst, now, reading))
                transitions += ctx.out
        return Step(tuple(evaluations), tuple(transitions))

    def _context(self, rule: Rule, signal: str) -> _Ctx:
        if rule.when == "equals":
            return _Ctx(rule, signal, None, None)
        limits = self._limits.get(signal)
        threshold = rule.threshold.resolve(limits)
        clear = rule.clear_threshold.resolve(limits)
        for spec, resolved in ((rule.threshold, threshold), (rule.clear_threshold, clear)):
            if resolved is None:
                return _Ctx(rule, signal, None, None, unavailable=f"no {spec.limit} limit")
        # A fixed value with a relative clear (or the reverse) can only be checked here.
        if (
            threshold is not None
            and clear is not None
            and clear_on_breaching_side(rule.when, threshold, clear)
        ):
            why = f"clear {clear:g} is on the breaching side of {threshold:g}"
            return _Ctx(rule, signal, None, None, unavailable=why)
        return _Ctx(rule, signal, threshold, clear)

    def _reading(self, rule: Rule, signal: str, signals: Mapping[str, Value], ctx: _Ctx) -> Value:
        """This poll's reading for one signal, after the guard.

        Precedence (docs/BIBLE.md, "Alerting"): a guard that is readable and false wins over a
        MISSING primary reading (the rule counts as clear); otherwise a MISSING primary or guard
        makes the reading MISSING. Named wildcards bind the guard to the sensor's device.
        """
        reading = signals.get(signal, Missing("no reading"))
        if rule.when != "equals" and isinstance(reading, str):
            reading = Missing(f"not a number: {reading!r}")
        guard = rule.only_if
        if guard is None:
            return reading
        pattern = globs.fill(guard.signal, rule.bindings(signal) or {})
        values = {s: v for s, v in signals.items() if globs.match(pattern, s) is not None}
        if any(not isinstance(v, Missing) and v == guard.equals for v in values.values()):
            return reading
        if not values:
            return Missing(f"guard {pattern}: no reading")
        missing = [(s, v) for s, v in values.items() if isinstance(v, Missing)]
        if missing:
            s, v = missing[0]
            return Missing(f"guard {s}: {v.reason}")
        ctx.guard_false = True
        return reading

    def _open(
        self,
        ctx: _Ctx,
        inst: _Instance,
        now: float,
        reason: str,
        value: float | str | None,
        *,
        no_data: bool = False,
    ) -> None:
        rule = ctx.rule
        severity = rule.missing_severity if no_data else rule.severity
        inst.alert = _Alert(now, severity, no_data, ctx.threshold)
        ctx.out.append(
            Transition(
                at=now,
                rule_id=rule.id,
                signal=ctx.signal,
                kind=TransitionKind.FIRING,
                severity=severity,
                reason=reason,
                value=value,
                threshold=ctx.threshold,
                opened_at=now,
                no_data=no_data,
            )
        )

    def _close(
        self, rule: Rule, signal: str, inst: _Instance, reason: str, value: float | str | None
    ) -> Transition:
        alert = inst.alert
        if alert is None:
            raise RuntimeError(f"{rule.id} {signal}: no open alert to close")
        inst.alert = None
        return Transition(
            at=self._clock(),
            rule_id=rule.id,
            signal=signal,
            kind=TransitionKind.RESOLVED,
            severity=alert.severity,
            reason=reason,
            value=value,
            threshold=alert.threshold,
            opened_at=alert.opened_at,
            no_data=alert.no_data,
        )

    def _record(
        self, ctx: _Ctx, inst: _Instance, now: float, reading: Value, state: State, reason: str
    ) -> Evaluation:
        inst.state = state
        missing = reading.reason if isinstance(reading, Missing) else None
        value = None if isinstance(reading, Missing) else reading
        return Evaluation(
            now,
            ctx.rule.id,
            ctx.signal,
            value,
            missing,
            state,
            reason,
            ctx.threshold,
            inst.alert.severity if inst.alert is not None else None,
            inst.alert.no_data if inst.alert is not None else False,
        )

    def _step(self, ctx: _Ctx, inst: _Instance, now: float, reading: Value) -> Evaluation:
        rule = ctx.rule
        # A step backwards (or a zero step) is no time passing.
        dt = max(now - inst.last_t, 0.0) if inst.last_real and inst.last_t is not None else 0.0

        if isinstance(reading, Missing) and not ctx.guard_false:
            return self._step_missing(ctx, inst, now, reading)
        # A real reading, or a false guard (which counts as clear even if the reading is missing).
        value = None if isinstance(reading, Missing) else reading
        if self._max_gap is not None and dt > self._max_gap and inst.last_t is not None:
            # Nobody looked during that gap: it is MISSING for that span, never breach time.
            if inst.missing_since is None:
                inst.missing_since = inst.last_t
            dt = 0.0

        # Real data again after a long gap: start over, like a resume.
        if inst.missing_since is not None:
            gap = now - inst.missing_since
            inst.missing_since = None
            if gap >= rule.missing_for_s:
                inst.restart()
                dt = 0.0
        # Data is back: a "no data" alert closes now, before any warm-up, which only concerns
        # alerts on values (ADV-015: after resume it stayed open for a whole warm-up window).
        if inst.alert is not None and inst.alert.no_data:
            ctx.out.append(self._close(rule, ctx.signal, inst, DATA_BACK, value))
        inst.last_t, inst.last_real = _later(inst.last_t, now), True

        breaching, past_clear = self._compare(ctx, reading)
        inst.breach_elapsed = inst.breach_elapsed + dt if breaching and inst.in_breach else 0.0
        inst.in_breach = breaching
        inst.clear_elapsed = inst.clear_elapsed + dt if past_clear and inst.in_clear else 0.0
        inst.in_clear = past_clear

        if inst.warming:
            window = 0.0 if rule.mode == "any" else rule.for_s
            inst.warm_elapsed += dt
            inst.warm_count += 1
            if inst.warm_elapsed < window:
                done = format_duration(inst.warm_elapsed)
                why = f"warming: {done} of {format_duration(window)}"
                return self._record(ctx, inst, now, reading, State.WARMING, why)
            inst.warming = False

        if inst.alert is not None:
            if past_clear and inst.clear_elapsed >= rule.clear_for_s:
                ctx.out.append(self._close(rule, ctx.signal, inst, RECOVERED, value))
                return self._record(ctx, inst, now, reading, State.RESOLVED, RECOVERED)
            else:
                why = f"firing; clear for {format_duration(inst.clear_elapsed)} of " + (
                    format_duration(rule.clear_for_s)
                )
                return self._record(ctx, inst, now, reading, State.FIRING, why)

        if (
            breaching
            and value is not None
            and (rule.mode == "any" or inst.breach_elapsed >= rule.for_s)
        ):
            reason = self._breach_text(ctx, value)
            self._open(ctx, inst, now, reason, value)
            return self._record(ctx, inst, now, reading, State.FIRING, reason)
        if breaching:
            why = f"breaching for {format_duration(inst.breach_elapsed)} of " + format_duration(
                rule.for_s
            )
            return self._record(ctx, inst, now, reading, State.PENDING, why)
        why = GUARD_FALSE if ctx.guard_false else "not breaching"
        return self._record(ctx, inst, now, reading, State.OK, why)

    def _step_missing(self, ctx: _Ctx, inst: _Instance, now: float, reading: Missing) -> Evaluation:
        rule = ctx.rule
        if inst.missing_since is None:
            inst.missing_since = now
        inst.last_t, inst.last_real = _later(inst.last_t, now), False
        gap = now - inst.missing_since
        if gap >= rule.missing_for_s:
            if rule.on_missing == "close" and inst.alert is not None:
                ctx.out.append(self._close(rule, ctx.signal, inst, CLOSED_NO_DATA, None))
                return self._record(ctx, inst, now, reading, State.RESOLVED, CLOSED_NO_DATA)
            if rule.on_missing == "alert" and inst.alert is None:
                reason = f"no data for {format_duration(rule.missing_for_s)}: {reading.reason}"
                self._open(ctx, inst, now, reason, None, no_data=True)
                return self._record(ctx, inst, now, reading, State.FIRING, reason)
        why = f"missing for {format_duration(gap)}: {reading.reason}"
        if inst.alert is not None:
            return self._record(ctx, inst, now, reading, State.FIRING, why)
        # Missing for longer than missing_for is Unknown, whatever on_missing says and even if
        # the rule never finished warming up: it is not still starting (bible "Unknown"; ADV-014).
        warming = inst.warming and gap < rule.missing_for_s
        state = State.WARMING if warming else State.UNKNOWN
        return self._record(ctx, inst, now, reading, state, why)

    @staticmethod
    def _compare(ctx: _Ctx, reading: Value) -> tuple[bool, bool]:
        """``(breaching, past clear)``. A false guard counts as fully clear."""
        if ctx.guard_false:
            return False, True
        rule = ctx.rule
        if rule.when == "equals":
            breaching = reading == rule.value
            return breaching, not breaching
        if isinstance(reading, Missing):  # only reachable with a false guard, handled above
            raise RuntimeError(f"{rule.id} {ctx.signal}: compared a missing reading")
        value = float(reading)
        if ctx.threshold is None or ctx.clear is None:  # _context() never allows this
            raise RuntimeError(f"{rule.id} {ctx.signal}: threshold unresolved")
        if rule.when == "above":
            return value > ctx.threshold, value <= ctx.clear
        return value < ctx.threshold, value >= ctx.clear

    @staticmethod
    def _breach_text(ctx: _Ctx, reading: float | str) -> str:
        rule = ctx.rule
        if rule.when == "equals":
            return f"equals {rule.value!r}"
        text = f"{float(reading):g} {rule.when} {ctx.threshold:g}"
        if rule.mode == "sustained" and rule.for_s > 0:
            text += f" for {format_duration(rule.for_s)}"
        return text
