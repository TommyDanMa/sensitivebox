"""Read-only history views. No Store construction, schema migration or file creation."""

import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import msgspec

from sensitivebox.alerts import Rule, State
from sensitivebox.alerts.model import Severity
from sensitivebox.store import schema
from sensitivebox.store.mutes import ActiveMute, Mutes


@dataclass(frozen=True, slots=True)
class StoredEvaluation:
    at: float
    rule_id: str
    signal: str
    state: State
    reason: str
    alert_severity: Severity | None
    no_data: bool
    metadata_known: bool = True


@dataclass(frozen=True, slots=True)
class History:
    evaluations: tuple[StoredEvaluation, ...] = ()
    interval: float = 1.0
    rules: tuple[Rule, ...] = ()
    reason: str | None = None


SELECT_LATEST = """
SELECT ts, rule_id, name, state, reason, alert_severity, no_data, metadata_known FROM (
    SELECT e.*, s.name, row_number() OVER (
        PARTITION BY e.rule_id, e.signal_id ORDER BY e.rowid DESC
    ) AS newest FROM evaluations_raw AS e JOIN signals AS s ON e.signal_id = s.id
) WHERE newest = 1
"""


def read_history(path: Path) -> History:
    if not path.exists():
        return History(reason="history missing; start sbox watch")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only = ON")
        db.execute("BEGIN")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if 1 <= version < schema.VERSION:
            return History(reason="history needs writer upgrade; start sbox watch")
        if version != schema.VERSION:
            raise sqlite3.DatabaseError("unsupported history schema; use a compatible sbox watch")
        row = db.execute(
            "SELECT value FROM inputs_raw WHERE kind = 'settings' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        interval = float(row[0]) / 3 if row and row[0] is not None else 1.0
        if not math.isfinite(interval) or interval <= 0:
            raise sqlite3.DatabaseError("invalid recorded poll interval; restart sbox watch")
        configured = db.execute(
            "SELECT payload FROM inputs_raw WHERE kind = 'rules' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        try:
            rules = tuple(msgspec.json.decode(configured[0], type=list[Rule])) if configured else ()
            evaluations: list[StoredEvaluation] = []
            for ts, rule_id, signal, state, reason, severity, no_data, known in db.execute(
                SELECT_LATEST
            ):
                if severity not in (None, "info", "warning", "critical") or not math.isfinite(ts):
                    raise ValueError("invalid evaluation")
                evaluations.append(
                    StoredEvaluation(
                        ts,
                        rule_id,
                        signal,
                        State(state),
                        reason,
                        severity,
                        bool(no_data),
                        bool(known),
                    )
                )
        except (ValueError, TypeError, msgspec.DecodeError) as exc:
            raise sqlite3.DatabaseError("invalid history data; restart sbox watch") from exc
        return History(tuple(evaluations), interval, rules)


def poll_interval(signal: str, fast: float) -> float:
    if signal.startswith("power_supply/"):
        return max(5.0, fast)
    if signal.startswith(("memory/", "vmstat/", "net/")):
        return max(2.0, fast)
    return fast


def read_mutes(path: Path, boot: str, now: float) -> tuple["ActiveMute", ...] | None:
    if not path.exists():
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA user_version").fetchone()[0] != schema.VERSION:
            return None
        return Mutes(db).active(boot, now)
