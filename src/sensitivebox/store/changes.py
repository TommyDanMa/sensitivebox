"""Persistent baselines, change markers and bounded apt cursors."""

import sqlite3
from dataclasses import dataclass

from sensitivebox.alerts.engine import Clock

CREATE = (
    "CREATE TABLE IF NOT EXISTS change_baselines (kind TEXT PRIMARY KEY,value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS change_markers (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
    identity TEXT UNIQUE)""",
    """CREATE TABLE IF NOT EXISTS apt_cursors (
    identity TEXT PRIMARY KEY, offset INTEGER NOT NULL, digest TEXT NOT NULL,
    pending TEXT NOT NULL, updated REAL NOT NULL)""",
)


@dataclass(frozen=True, slots=True)
class ChangeMarker:
    id: int
    ts: float
    kind: str
    value: str


class Changes:
    def __init__(self, db: sqlite3.Connection, clock: Clock) -> None:
        self.db, self.clock = db, clock

    def observe(self, kind: str, value: str) -> None:
        if not value:
            return
        with self.db:
            row = self.db.execute(
                "SELECT value FROM change_baselines WHERE kind=?", (kind,)
            ).fetchone()
            if row and row[0] != value:
                self.marker(kind, value)
            self.db.execute("INSERT OR REPLACE INTO change_baselines VALUES(?,?)", (kind, value))

    def marker(self, kind: str, value: str, identity: str | None = None) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO change_markers(ts,kind,value,identity) VALUES(?,?,?,?)",
            (self.clock(), kind, value, identity),
        )

    def markers(self, limit: int = 1000) -> list[ChangeMarker]:
        return [
            ChangeMarker(*r)
            for r in self.db.execute(
                "SELECT id,ts,kind,value FROM change_markers ORDER BY id DESC LIMIT ?", (limit,)
            )
        ]

    def prune(self, now: float) -> None:
        self.db.execute("DELETE FROM change_markers WHERE ts<?", (now - 400 * 86400,))
        self.db.execute("DELETE FROM apt_cursors WHERE updated<?", (now - 400 * 86400,))
