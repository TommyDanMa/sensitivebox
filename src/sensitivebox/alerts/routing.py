"""From transitions to notifications: engine → **mute** → cooldown → notifiers.

Muting sits after the engine, never inside it: a muted alert still evaluates, is logged and lights
the LED; only its notification is held back. Prompt 2 ships the ``Mute`` interface and a
pass-through stub; the real layer (dashboard mutes, quiet hours, re-notify when a mute ends) is
Prompt 3.

Cooldown is the minimum gap between repeat firing notifications of the same rule, signal and kind
of alert (a value alert, or a "no data" one). It never holds back a more severe alert than the last
one delivered, so an info "no data" notification can't mask a critical heat alert (ADV-001).
"""

import socket
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Protocol

import msgspec

from sensitivebox.alerts.engine import Transition, TransitionKind, format_duration
from sensitivebox.alerts.model import Rule, Severity

SEVERITY_RANK: dict[Severity, int] = {"info": 0, "warning": 1, "critical": 2}


@dataclass(frozen=True, slots=True)
class Notification:
    at: float
    rule_id: str
    signal: str
    kind: TransitionKind
    severity: Severity
    title: str  # "CRITICAL · CPU hot · proart-p16"
    body: str  # "95 above 90 for 10 s (k10temp/0000:00:18.3/temp1)"
    opened_at: float  # identifies the alert this belongs to
    no_data: bool = False


@dataclass(frozen=True, slots=True)
class MuteDecision:
    deliver: bool
    reason: str | None = None  # why it was held
    renotify: bool = True
    manual_release: str | None = None


class Mute(Protocol):
    """Decides, per notification, whether it goes out now. Never sees or changes alert state."""

    def decide(self, notification: Notification) -> MuteDecision: ...


class PassThroughMute:
    """The Prompt 2 stub: every notification is delivered."""

    def decide(self, notification: Notification) -> MuteDecision:
        return MuteDecision(deliver=True)


@dataclass(frozen=True, slots=True)
class Held:
    notification: Notification
    reason: str


@dataclass(frozen=True, slots=True)
class Routed:
    delivered: tuple[Notification, ...] = ()
    held: tuple[Held, ...] = ()


def title(severity: Severity, rule_title: str, host: str) -> str:
    return f"{severity.upper()} · {rule_title} · {host}"


def body(transition: Transition) -> str:
    reason = transition.reason[:1].upper() + transition.reason[1:]
    if transition.kind is TransitionKind.RESOLVED:
        reason += f" after {format_duration(transition.at - transition.opened_at)}"
    return f"{reason} ({transition.signal})"


@dataclass(frozen=True, slots=True)
class RouterMemory:
    last: list[tuple[str, str, bool, float, Severity]]
    cooled: list[tuple[str, str, float]]
    alerts: list[tuple[Transition, MuteDecision | None, bool]]


@dataclass(slots=True)
class Router:
    host: str = field(default_factory=socket.gethostname)
    mute: Mute = field(default_factory=PassThroughMute)
    # (rule id, signal, no-data?) → when and at what severity its last firing notification went out
    _last_fired: dict[tuple[str, str, bool], tuple[float, Severity]] = field(
        default_factory=lambda: {}
    )
    # alerts whose firing notification was held by cooldown, so their resolve is held too
    _cooled: set[tuple[str, str, float]] = field(default_factory=lambda: set())

    _open: dict[tuple[str, str, float, bool], Transition] = field(default_factory=lambda: {})
    _blocked: dict[tuple[str, str, float, bool], MuteDecision] = field(default_factory=lambda: {})
    _pending: set[tuple[str, str, float, bool]] = field(default_factory=lambda: set())

    def open_alerts(self) -> tuple[Transition, ...]:
        return tuple(self._open.values())

    def checkpoint(self) -> str:
        memory = RouterMemory(
            [(r, s, n, at, severity) for (r, s, n), (at, severity) in self._last_fired.items()],
            list(self._cooled),
            [(t, self._blocked.get(key), key in self._pending) for key, t in self._open.items()],
        )
        return msgspec.json.encode(memory).decode()

    def restore(self, payload: str) -> None:
        memory = msgspec.json.decode(payload, type=RouterMemory)
        self._last_fired = {(r, s, n): (at, severity) for r, s, n, at, severity in memory.last}
        self._cooled = set(memory.cooled)
        self._open.clear()
        self._blocked.clear()
        self._pending.clear()
        for t, decision, pending in memory.alerts:
            key = (t.rule_id, t.signal, t.opened_at, t.no_data)
            self._open[key] = t
            if decision is not None:
                self._blocked[key] = decision
            if pending:
                self._pending.add(key)

    def poll(
        self,
        at: float,
        rules: Mapping[str, Rule],
        *,
        defer_delivery: bool = False,
        skip: Iterable[Notification] = (),
    ) -> Routed:
        delivered: list[Notification] = []
        held: list[Held] = []
        skipped = {(n.rule_id, n.signal, n.opened_at, n.no_data) for n in skip}
        for key, original in list(self._open.items()):
            if key in skipped:
                continue
            t = replace(original, at=at)
            note = self._notification(t, rules.get(t.rule_id))
            decision = self.mute.decide(note)
            if not decision.deliver:
                if key not in self._blocked:
                    held.append(Held(note, decision.reason or "muted"))
                self._blocked[key] = decision
                continue
            previous = self._blocked.pop(key, None)
            if previous is not None and (
                previous.renotify or decision.manual_release == previous.reason
            ):
                self._pending.add(key)
            if key not in self._pending:
                continue
            reason = self._cooldown(t, rules.get(t.rule_id))
            if reason is None:
                delivered.append(note)
                if not defer_delivery:
                    self.confirm_delivery(note)
            elif previous is not None:
                held.append(Held(note, reason))
        return Routed(tuple(delivered), tuple(held))

    def _notification(self, t: Transition, rule: Rule | None) -> Notification:
        return Notification(
            at=t.at,
            rule_id=t.rule_id,
            signal=t.signal,
            kind=t.kind,
            severity=t.severity,
            title=title(t.severity, rule.title if rule else t.rule_id, self.host),
            body=body(t),
            opened_at=t.opened_at,
            no_data=t.no_data,
        )

    def route(
        self,
        transitions: Iterable[Transition],
        rules: Mapping[str, Rule],
        *,
        defer_delivery: bool = False,
    ) -> Routed:
        delivered: list[Notification] = []
        held: list[Held] = []
        for t in transitions:
            rule = rules.get(t.rule_id)
            key = (t.rule_id, t.signal, t.opened_at, t.no_data)
            if t.kind is TransitionKind.FIRING:
                self._open[key] = t
            else:
                self._open.pop(key, None)
                self._blocked.pop(key, None)
                self._pending.discard(key)
            note = self._notification(t, rule)
            decision = self.mute.decide(note)
            if not decision.deliver:
                if t.kind is TransitionKind.FIRING:
                    self._blocked[key] = decision
                held.append(Held(note, decision.reason or "muted"))
                continue
            reason = self._cooldown(t, rule)
            if reason is not None:
                if t.kind is TransitionKind.FIRING:
                    self._pending.add(key)
                held.append(Held(note, reason))
                continue
            delivered.append(note)
            if t.kind is TransitionKind.FIRING:
                self._pending.add(key)
            if not defer_delivery:
                self.confirm_delivery(note)
        return Routed(tuple(delivered), tuple(held))

    def confirm_delivery(self, note: Notification, *, at: float | None = None) -> None:
        """Commit a successful firing send. Until then it remains eligible for retry."""
        if note.kind is TransitionKind.FIRING:
            self._last_fired[(note.rule_id, note.signal, note.no_data)] = (
                note.at if at is None else at,
                note.severity,
            )
            self._pending.discard((note.rule_id, note.signal, note.opened_at, note.no_data))
            self._cooled.discard((note.rule_id, note.signal, note.opened_at))

    def _cooldown(self, t: Transition, rule: Rule | None) -> str | None:
        key = (t.rule_id, t.signal, t.no_data)
        alert = (t.rule_id, t.signal, t.opened_at)
        if t.kind is TransitionKind.RESOLVED:
            if alert in self._cooled:
                self._cooled.discard(alert)
                return "cooldown"
            return None
        last = self._last_fired.get(key)
        cooldown = rule.cooldown_s if rule is not None else 0.0
        if last is not None:
            last_at, last_severity = last
            elapsed = max(t.at - last_at, 0.0)  # a clock step backwards is no time passing
            escalated = SEVERITY_RANK[t.severity] > SEVERITY_RANK[last_severity]
            if elapsed < cooldown and not escalated:
                self._cooled.add(alert)
                return "cooldown"
        return None
