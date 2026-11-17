"""A heartbeat summary from the poll just collected; no additional reads or processes."""

from collections import Counter
from dataclasses import dataclass

from sensitivebox.alerts import State, format_duration
from sensitivebox.daemon import TickResult
from sensitivebox.store.mutes import ActiveMute


@dataclass(frozen=True, slots=True)
class LiveStatus:
    at: float
    memory_used_pct: float | None
    memory_pressure: float | None
    open_alerts: dict[str, int]
    states: dict[str, int]
    muted: tuple[ActiveMute, ...] | None = None


def live_status(result: TickResult) -> LiveStatus:
    counts = Counter(e.alert_severity for e in result.step.evaluations if e.alert_severity)
    available = result.snapshot.signals.get("memory/available_pct")
    pressure = result.snapshot.signals.get("psi/memory/some_avg10")
    return LiveStatus(
        result.snapshot.at,
        100 - available if isinstance(available, (int, float)) else None,
        pressure if isinstance(pressure, (int, float)) else None,
        {s: counts[s] for s in ("info", "warning", "critical")},
        {s.value: sum(e.state == s for e in result.step.evaluations) for s in State},
        result.muted,
    )


def render_live(view: LiveStatus) -> str:
    memory = f"{view.memory_used_pct:.0f} %" if view.memory_used_pct is not None else "unavailable"
    pressure = f"{view.memory_pressure:.1f}" if view.memory_pressure is not None else "unavailable"
    alerts = " · ".join(f"{n} {s}" for s, n in view.open_alerts.items() if n) or "Nothing firing"
    states = " · ".join(f"{view.states[s]} {s}" for s in ("unknown", "warming", "pending"))
    muted = "muted unavailable" if view.muted is None else f"{len(view.muted)} muted"
    if view.muted:
        muted += (
            " ("
            + ", ".join(
                m.rule_id
                + ": "
                + (
                    "until reboot"
                    if m.remaining_s is None
                    else format_duration(m.remaining_s) + " left"
                )
                for m in view.muted
            )
            + ")"
        )
    return f"Status · Memory {memory} · PSI {pressure} · {alerts} · {muted} · {states}"
