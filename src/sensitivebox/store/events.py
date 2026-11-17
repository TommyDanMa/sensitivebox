"""Incident facts, routed events and quality counters. Only the daemon is a writer."""

import math
import sqlite3
import statistics
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import tzinfo

import msgspec

from sensitivebox.alerts.engine import Clock, Evaluation, TransitionKind
from sensitivebox.alerts.model import Rule, Severity
from sensitivebox.alerts.routing import Notification, Routed
from sensitivebox.store.quality import Weekly, local_zone, week_at, week_spans

RETENTION_S = 400 * 86400
CREATE = (
    """CREATE TABLE IF NOT EXISTS alert_incidents (
    id TEXT PRIMARY KEY, rule_id TEXT NOT NULL, signal TEXT NOT NULL, no_data INTEGER NOT NULL,
    opened REAL NOT NULL, closed REAL, active_s REAL NOT NULL DEFAULT 0,
    incomplete INTEGER NOT NULL DEFAULT 0, muted INTEGER NOT NULL DEFAULT 0,
    acknowledged INTEGER NOT NULL DEFAULT 0)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS alert_open ON alert_incidents(rule_id, signal, no_data)
    WHERE closed IS NULL""",
    """CREATE TABLE IF NOT EXISTS incident_summaries (
    incident_id TEXT PRIMARY KEY REFERENCES alert_incidents(id) ON DELETE CASCADE,
    name TEXT NOT NULL, severity TEXT NOT NULL, peak REAL, unit TEXT NOT NULL DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS alert_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT NOT NULL REFERENCES alert_incidents(id),
    ts REAL NOT NULL, kind TEXT NOT NULL, reason TEXT NOT NULL, notification TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS alert_events_ts ON alert_events(ts)",
    """CREATE TABLE IF NOT EXISTS incident_calendar (
    incident_id TEXT PRIMARY KEY REFERENCES alert_incidents(id) ON DELETE CASCADE,
    week TEXT NOT NULL, zone TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS weekly_quality (
    rule_id TEXT NOT NULL, week TEXT NOT NULL, zone TEXT NOT NULL, updated REAL NOT NULL,
    fires INTEGER NOT NULL DEFAULT 0, seconds REAL NOT NULL DEFAULT 0,
    durations TEXT NOT NULL DEFAULT '[]', incomplete INTEGER NOT NULL DEFAULT 0,
    muted INTEGER NOT NULL DEFAULT 0, acknowledged INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(rule_id, week, zone))""",
    "CREATE TABLE IF NOT EXISTS operations_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)


@dataclass(frozen=True, slots=True)
class Incident:
    id: str
    rule_id: str
    signal: str
    no_data: bool
    opened: float
    closed: float | None
    active_s: float
    incomplete: bool
    muted: bool
    acknowledged: bool

    def __post_init__(self) -> None:
        # SQLite persists flags as 0/1; public JSON and typed consumers require booleans.
        for name in ("no_data", "incomplete", "muted", "acknowledged"):
            object.__setattr__(self, name, bool(getattr(self, name)))


@dataclass(frozen=True, slots=True)
class AlertEvent:
    id: int
    incident_id: str
    ts: float
    kind: str
    reason: str
    notification: Notification


@dataclass(frozen=True, slots=True)
class IncidentSummary:
    name: str
    severity: Severity
    peak: float | None
    unit: str


class Events:
    def __init__(self, db: sqlite3.Connection, clock: Clock, *, writer: bool = True) -> None:
        self.db, self.clock = db, clock
        self.zone: Callable[[], tzinfo] = local_zone
        self._last: tuple[float, float] | None = None
        if writer:
            with db:
                db.execute("UPDATE alert_incidents SET incomplete=1 WHERE closed IS NULL")

    def _week(self, rule: str, ts: float) -> tuple[str, str, str]:
        week, zone = week_at(ts, self.zone())
        self.db.execute(
            "INSERT OR IGNORE INTO weekly_quality(rule_id,week,zone,updated) VALUES (?,?,?,?)",
            (rule, week, zone, self.clock()),
        )
        return rule, week, zone

    def pause(self) -> None:
        self._last = None

    def observe(self, mono: float, max_gap: float | None = None) -> None:
        now = self.clock()
        previous = self._last
        if previous is not None:
            mono = max(mono, previous[0])
        self._last = (mono, now)
        if previous is None:
            return
        elapsed = max(0, mono - previous[0])
        with self.db:
            if max_gap is not None and elapsed > max_gap:
                self.db.execute("UPDATE alert_incidents SET incomplete=1 WHERE closed IS NULL")
                return
            for incident in self.incidents(open_only=True):
                self.db.execute(
                    "UPDATE alert_incidents SET active_s=active_s+? WHERE id=?",
                    (elapsed, incident.id),
                )
                # Attribute elapsed monotonic time to the civil weeks occupied at this poll.
                for week, zone, seconds in week_spans(now - elapsed, now, self.zone()):
                    self.db.execute(
                        "INSERT OR IGNORE INTO weekly_quality(rule_id,week,zone,updated)"
                        " VALUES (?,?,?,?)",
                        (incident.rule_id, week, zone, now),
                    )
                    self.db.execute(
                        "UPDATE weekly_quality SET seconds=seconds+?,updated=?"
                        " WHERE rule_id=? AND week=? AND zone=?",
                        (seconds, now, incident.rule_id, week, zone),
                    )

    def record(self, routed: Routed) -> None:
        with self.db:
            for note in routed.delivered:
                self._record(note, "delivered")
            for held in routed.held:
                self._record(held.notification, held.reason)

    def sample(
        self,
        evaluations: Iterable[Evaluation],
        rules: Mapping[str, Rule],
        units: Mapping[str, str],
    ) -> None:
        """Keep the worst real value of each open incident beyond the raw ring's lifetime.

        Above rules retain their maximum; below rules retain their minimum. Missing and
        no-data readings never become a peak. Recovery readings cannot replace the extreme.
        """
        with self.db:
            for evaluation in evaluations:
                rule = rules.get(evaluation.rule_id)
                if rule is None:
                    continue
                row = self.db.execute(
                    "SELECT i.id,s.peak,s.unit FROM alert_incidents i"
                    " JOIN incident_summaries s ON s.incident_id=i.id"
                    " WHERE i.rule_id=? AND i.signal=? AND i.no_data=? AND i.closed IS NULL",
                    (evaluation.rule_id, evaluation.signal, evaluation.no_data),
                ).fetchone()
                if row is None:
                    continue
                peak: float | None = row[1]
                value = evaluation.value
                if (
                    not evaluation.no_data
                    and evaluation.missing is None
                    and isinstance(value, (int, float))
                    and math.isfinite(value)
                    and (rule.when != "equals" or value == evaluation.threshold)
                ):
                    peak = (
                        value
                        if peak is None
                        else min(peak, value)
                        if rule.when == "below"
                        else max(peak, value)
                    )
                self.db.execute(
                    "UPDATE incident_summaries SET name=?,peak=?,unit=? WHERE incident_id=?",
                    (rule.title, peak, units.get(evaluation.signal, row[2]), row[0]),
                )

    def _record(self, note: Notification, reason: str) -> None:
        now = self.clock()
        row = self.db.execute(
            "SELECT id FROM alert_incidents WHERE rule_id=? AND signal=? AND no_data=?"
            " AND closed IS NULL",
            (note.rule_id, note.signal, note.no_data),
        ).fetchone()
        if row is None:
            if note.kind is not TransitionKind.FIRING:
                return
            incident = uuid.uuid4().hex
            self.db.execute(
                "INSERT INTO alert_incidents(id,rule_id,signal,no_data,opened) VALUES(?,?,?,?,?)",
                (incident, note.rule_id, note.signal, note.no_data, now),
            )
            self.db.execute(
                "INSERT INTO incident_summaries(incident_id,name,severity) VALUES(?,?,?)",
                (incident, note.rule_id, note.severity),
            )
            key = self._week(note.rule_id, now)
            self.db.execute(
                "INSERT INTO incident_calendar VALUES(?,?,?)", (incident, key[1], key[2])
            )
            self.db.execute(
                "UPDATE weekly_quality SET fires=fires+1 WHERE rule_id=? AND week=? AND zone=?",
                key,
            )
            kind = "firing"
        else:
            incident = str(row[0])
            kind = "renotify" if note.kind is TransitionKind.FIRING else "resolved"
        self.db.execute(
            "INSERT INTO alert_events(incident_id,ts,kind,reason,notification) VALUES(?,?,?,?,?)",
            (incident, now, kind, reason, msgspec.json.encode(note).decode()),
        )
        if reason not in ("delivered", "cooldown", "delivery failed"):
            self.fact(incident, "muted")
        if note.kind is TransitionKind.RESOLVED:
            current = next(i for i in self.incidents(open_only=True) if i.id == incident)
            key = self._week(note.rule_id, now)
            durations = msgspec.json.decode(
                self.db.execute(
                    "SELECT durations FROM weekly_quality WHERE rule_id=? AND week=? AND zone=?",
                    key,
                ).fetchone()[0],
                type=list[float],
            )
            durations.append(current.active_s)
            self.db.execute(
                "UPDATE weekly_quality SET durations=?,incomplete=incomplete+?"
                " WHERE rule_id=? AND week=? AND zone=?",
                (msgspec.json.encode(durations).decode(), int(current.incomplete), *key),
            )
            self.db.execute("UPDATE alert_incidents SET closed=? WHERE id=?", (now, incident))

    def fact(self, incident_id: str, fact: str) -> None:
        if fact not in ("muted", "acknowledged"):
            raise ValueError("unknown incident fact")
        incident = next(i for i in self.incidents() if i.id == incident_id)
        if incident.muted if fact == "muted" else incident.acknowledged:
            return
        calendar = self.db.execute(
            "SELECT week,zone FROM incident_calendar WHERE incident_id=?", (incident_id,)
        ).fetchone()
        if calendar is None:  # older writer data gets a calendar on its next fact
            key = self._week(incident.rule_id, incident.opened)
            self.db.execute(
                "INSERT INTO incident_calendar VALUES(?,?,?)", (incident_id, key[1], key[2])
            )
        else:
            key = (incident.rule_id, str(calendar[0]), str(calendar[1]))
        if fact == "muted":
            self.db.execute("UPDATE alert_incidents SET muted=1 WHERE id=?", (incident_id,))
            self.db.execute(
                "UPDATE weekly_quality SET muted=muted+1 WHERE rule_id=? AND week=? AND zone=?", key
            )
        else:
            self.db.execute("UPDATE alert_incidents SET acknowledged=1 WHERE id=?", (incident_id,))
            self.db.execute(
                "INSERT OR REPLACE INTO operations_meta VALUES ('acknowledgement_producer','1')"
            )
            self.db.execute(
                "UPDATE weekly_quality SET acknowledged=acknowledged+1"
                " WHERE rule_id=? AND week=? AND zone=?",
                key,
            )

    def incidents(self, *, open_only: bool = False) -> list[Incident]:
        query = "SELECT * FROM alert_incidents"
        if open_only:
            query += " WHERE closed IS NULL"
        return [Incident(*r) for r in self.db.execute(query + " ORDER BY opened DESC")]

    def history(
        self, since: float = 0, limit: int = 1001, after: int = 0, incident: str | None = None
    ) -> list[AlertEvent]:
        return [
            AlertEvent(r[0], r[1], r[2], r[3], r[4], msgspec.json.decode(r[5], type=Notification))
            for r in self.db.execute(
                "SELECT * FROM alert_events WHERE ts>=? AND id>?"
                " AND (? IS NULL OR incident_id=?) ORDER BY id DESC LIMIT ?",
                (since, after, incident, incident, limit),
            )
        ]

    def weekly(self) -> list[Weekly]:
        ack = (
            self.db.execute(
                "SELECT 1 FROM operations_meta WHERE key='acknowledgement_producer'"
            ).fetchone()
            is not None
        )
        result: list[Weekly] = []
        for (
            rule,
            week,
            zone,
            _,
            fires,
            seconds,
            encoded,
            incomplete,
            muted,
            acknowledged,
        ) in self.db.execute("SELECT * FROM weekly_quality ORDER BY week DESC,rule_id,zone"):
            durations = msgspec.json.decode(encoded, type=list[float])
            result.append(
                Weekly(
                    rule,
                    week,
                    zone,
                    fires,
                    seconds / 60,
                    statistics.median(durations) if durations else None,
                    100 * sum(d < 300 for d in durations) / len(durations) if durations else None,
                    100 * acknowledged / fires if ack and fires else None,
                    100 * muted / fires if fires else None,
                    incomplete,
                )
            )
        return result

    def prune(self, now: float) -> None:
        cutoff = now - RETENTION_S
        self.db.execute(
            "DELETE FROM alert_events WHERE ts<? AND incident_id NOT IN"
            " (SELECT id FROM alert_incidents WHERE closed IS NULL)",
            (cutoff,),
        )
        self.db.execute("DELETE FROM alert_incidents WHERE closed<?", (cutoff,))
        self.db.execute("DELETE FROM weekly_quality WHERE updated<?", (cutoff,))
