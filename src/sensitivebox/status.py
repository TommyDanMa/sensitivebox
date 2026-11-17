"""Typed status and per-signal rule views, without starting a new alert engine."""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from sensitivebox.alerts import Rule, State, format_duration
from sensitivebox.alerts.model import Severity
from sensitivebox.doctor import format_bytes
from sensitivebox.hwmon import Group, Kind, ReadStatus
from sensitivebox.sample import Sample
from sensitivebox.store.db import FLUSH_EVERY_S
from sensitivebox.store.mutes import ActiveMute
from sensitivebox.store.status import History, poll_interval


@dataclass(frozen=True, slots=True)
class RuleRow:
    rule_id: str
    signal: str | None
    state: State
    reason: str
    fresh: bool
    alert_severity: Severity | None = None
    no_data: bool = False


@dataclass(frozen=True, slots=True)
class RuleView:
    rules: tuple[RuleRow, ...]


def rule_rows(
    rules: Sequence[Rule], sample: Sample, history: History, now: float
) -> tuple[RuleRow, ...]:
    stored = {(e.rule_id, e.signal): e for e in history.evaluations}
    loaded = {r.id: r for r in history.rules}
    rows: list[RuleRow] = []
    for rule in rules:
        matched = {s for s in sample.signals if rule.matches(s)}
        matched.update(s for rid, s in stored if rid == rule.id and rule.matches(s))
        if not matched:
            rows.append(RuleRow(rule.id, None, State.UNKNOWN, "no matching signal", True))
        for signal in sorted(matched):
            entry = stored.get((rule.id, signal))
            fresh = (
                entry is not None
                and entry.metadata_known
                and loaded.get(rule.id) == rule
                and 0
                <= now - entry.at
                <= 3 * poll_interval(signal, history.interval) + FLUSH_EVERY_S
            )
            if fresh and entry is not None:
                rows.append(
                    RuleRow(
                        rule.id,
                        signal,
                        entry.state,
                        entry.reason,
                        True,
                        entry.alert_severity,
                        entry.no_data,
                    )
                )
            else:
                reason = history.reason or "alert state stale or missing; start sbox watch"
                if entry is not None and loaded.get(rule.id) != rule:
                    reason = "configured rule differs from daemon; restart sbox watch"
                rows.append(RuleRow(rule.id, signal, State.UNKNOWN, reason, False))
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class Readings:
    temperatures: dict[str, float]
    fans: dict[str, float]
    memory_used_pct: float | None
    memory_pressure: float | None
    swap_bytes: int | None


@dataclass(frozen=True, slots=True)
class Status:
    host: str
    host_uptime: float | None
    health: int
    fresh: bool
    readings: Readings
    open_alerts: dict[str, int]
    states: dict[str, int]
    rules_watching: int
    hint: str | None
    alerts: tuple[RuleRow, ...]
    muted: tuple[ActiveMute, ...] | None = None
    network_today: None = None
    network_month: None = None


def status(sample: Sample, rows: Sequence[RuleRow]) -> Status:
    active = Counter(r.alert_severity for r in rows if r.fresh and r.alert_severity is not None)
    fresh = any(r.signal and r.fresh for r in rows) and all(r.fresh for r in rows)
    health = 2 if active["critical"] else 1 if active["warning"] or not fresh else 0
    temperatures: dict[str, float] = {}
    fans: dict[str, float] = {}
    for chip in sample.chips:
        for sensor in chip.sensors:
            value = sample.readings[sensor.id]
            if value.status is ReadStatus.OK and value.value is not None:
                if sensor.kind is Kind.TEMP:
                    temperatures[sensor.id] = value.value
                elif sensor.kind is Kind.FAN:
                    fans[sensor.id] = value.value
    memory = sample.sources.meminfo
    pressure = sample.sources.pressure.get("memory")
    readings = Readings(
        temperatures,
        fans,
        memory.used_fraction * 100 if memory else None,
        pressure.some.avg10 if pressure else None,
        memory.swap_used if memory else None,
    )
    return Status(
        sample.host,
        sample.uptime,
        health,
        bool(fresh),
        readings,
        {s: active[s] for s in ("info", "warning", "critical")},
        {s.value: sum(r.state == s for r in rows) for s in State},
        len({r.rule_id for r in rows if r.signal is not None}),
        None if fresh else "alert state stale or missing; start sbox watch",
        tuple(r for r in rows if r.fresh and r.alert_severity is not None),
    )


def render_status(sample: Sample, view: Status) -> str:
    state = (
        "critical"
        if view.health == 2
        else "warning"
        if view.open_alerts["warning"]
        else "ok"
        if view.fresh
        else "unknown"
    )
    uptime = format_duration(view.host_uptime) if view.host_uptime is not None else "unavailable"
    temperatures: list[str] = []
    for group in (Group.CPU, Group.GPU, Group.STORAGE):
        values = [
            view.readings.temperatures[s.id]
            for c in sample.chips
            for s in c.sensors
            if s.group == group and s.id in view.readings.temperatures
        ]
        label = "NVMe" if group is Group.STORAGE else group.value
        temperatures.append(f"{label} {max(values):g} °C" if values else f"{label} unavailable")
    fans = "/".join(f"{v / 1000:.1f}k" for v in view.readings.fans.values())
    temperatures.append(f"Fans {fans} RPM" if fans else "Fans unavailable")
    reading = view.readings
    memory = (
        f"{reading.memory_used_pct:.0f} %" if reading.memory_used_pct is not None else "unavailable"
    )
    pressure = (
        f"{reading.memory_pressure:.1f}" if reading.memory_pressure is not None else "unavailable"
    )
    swap = format_bytes(reading.swap_bytes) if reading.swap_bytes is not None else "unavailable"
    active = " · ".join(f"{n} {s}" for s, n in view.open_alerts.items() if n) or "Nothing firing"
    counts = " · ".join(f"{view.states[s]} {s}" for s in ("unknown", "warming", "pending"))
    muted = (
        "unavailable"
        if view.muted is None
        else str(len(view.muted))
        + " ("
        + ", ".join(
            f"{m.rule_id}, "
            + (
                "until reboot"
                if m.remaining_s is None
                else f"{format_duration(m.remaining_s)} left"
            )
            for m in view.muted
        )
        + ")"
    )
    lines = [
        f"{view.host} · {state} · host uptime {uptime}",
        "   ".join(temperatures),
        f"Memory {memory}  pressure {pressure}  Swap {swap}  "
        "↓/↑ today: unavailable · month: unavailable",
        f"{active} · {view.rules_watching} rules watching · muted: {muted} · {counts}",
    ]
    lines.extend(f"{r.alert_severity} · {r.rule_id} · {r.signal}: {r.reason}" for r in view.alerts)
    if view.hint:
        lines.append(view.hint)
    return "\n".join(lines)


def render_rules(rows: Sequence[RuleRow]) -> str:
    return "\n".join(
        [
            "RULE  SIGNAL  STATE  REASON",
            *(f"{r.rule_id}  {r.signal or '—'}  {r.state.value}  {r.reason}" for r in rows),
        ]
    )
