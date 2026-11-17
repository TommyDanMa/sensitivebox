"""Bounded SQLite queries on the daemon thread, including pending raw/rollup data."""

import math
import sqlite3
from dataclasses import dataclass, replace

from sensitivebox.api.model import AlertsView, IncidentView


@dataclass(frozen=True, slots=True)
class RawHistory:
    t: tuple[float, ...]
    avg: tuple[float | None, ...]


@dataclass(frozen=True, slots=True)
class MinuteHistory(RawHistory):
    min: tuple[float | None, ...]
    max: tuple[float | None, ...]


def history(
    db: sqlite3.Connection, signal: str, period: str, now: float, interval: float
) -> RawHistory | MinuteHistory:
    """Caller flushes once before reading. Never combine rollups with raw samples."""
    width = {"1h": 3600, "6h": 21600, "24h": 86400}[period]
    if period != "1h":
        start, end = math.floor((now - width) / 60) * 60, math.floor(now / 60) * 60
        buckets: dict[int, tuple[float, float, float]] = {
            int(t): (float(total) / n, float(lo), float(hi))
            for t, n, total, lo, hi in db.execute(
                "SELECT bucket,n,total,lo,hi FROM rollup_1m WHERE signal_id="
                " (SELECT id FROM signals WHERE name=?) AND bucket BETWEEN ? AND ?",
                (signal, start, end),
            )
            if n > 0
        }
        bucket_times = tuple(float(t) for t in range(start, end + 1, 60))
        values = [buckets.get(int(t), (None, None, None)) for t in bucket_times]
        return MinuteHistory(
            bucket_times,
            tuple(v[0] for v in values),
            tuple(v[1] for v in values),
            tuple(v[2] for v in values),
        )

    # Left-join poll frames: an absent value in a frame is missing too. Metric rows take
    # precedence only when no signal value exists. UNIX wall timestamps, never engine mono.
    rows: list[tuple[float, float | None]] = db.execute(
        "SELECT tick.ts,coalesce(v.value,m.value) FROM inputs_raw tick"
        " LEFT JOIN inputs_raw v ON v.frame=tick.frame AND v.kind='value'"
        " AND v.signal_id=(SELECT id FROM signals WHERE name=?)"
        " LEFT JOIN metrics_raw m ON m.ts=tick.ts"
        " AND m.signal_id=(SELECT id FROM signals WHERE name=?)"
        " WHERE tick.kind='tick' AND tick.ts BETWEEN ? AND ? ORDER BY tick.ts,tick.frame",
        (signal, signal, now - width, now),
    ).fetchall()
    times: list[float] = []
    values_raw: list[float | None] = []
    for at, value in rows:
        if times:
            gap = times[-1] + interval
            while gap < at - interval / 2:
                times.append(gap)
                values_raw.append(None)
                gap += interval
        times.append(at)
        values_raw.append(value)
    return RawHistory(tuple(times), tuple(values_raw))


_INCIDENT = (
    "SELECT i.*,s.name,s.severity,s.peak,s.unit FROM alert_incidents i"
    " LEFT JOIN incident_summaries s ON s.incident_id=i.id"
)


def incidents(
    db: sqlite3.Connection, now: float, limit: int, *, boot: str, monotonic_now: float
) -> AlertsView:
    rows = db.execute(
        _INCIDENT + " WHERE i.closed IS NULL OR i.closed>=?"
        " ORDER BY i.opened DESC,i.id DESC LIMIT ?",
        (now - 86400, limit + 1),
    ).fetchall()
    muted_rules = _active_mutes(db, boot, monotonic_now)
    shown: list[IncidentView] = []
    for row in rows[:limit]:
        incident = IncidentView(*row)
        if incident.closed is None and incident.rule_id in muted_rules:
            incident = replace(incident, muted=True)
        shown.append(incident)
    return AlertsView(tuple(shown), len(rows) > limit)


def _active_mutes(db: sqlite3.Connection, boot: str, now: float) -> set[str]:
    return {
        str(rule)
        for (rule,) in db.execute(
            "SELECT rule_id FROM mutes WHERE boot=? AND released=0"
            " AND (expiry IS NULL OR expiry>?)",
            (boot, now),
        )
    }


def incident(db: sqlite3.Connection, identity: str) -> IncidentView | None:
    row = db.execute(_INCIDENT + " WHERE i.id=?", (identity,)).fetchone()
    return None if row is None else IncidentView(*row)
