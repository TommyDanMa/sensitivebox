"""Small SQLite control plane shared by the CLI and running daemon."""

import re
import sqlite3
from dataclasses import dataclass

CREATE = (
    """CREATE TABLE IF NOT EXISTS mutes (
    rule_id TEXT PRIMARY KEY, boot TEXT NOT NULL, expiry REAL, renotify INTEGER NOT NULL,
    released INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS quiet_hours (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), schedule TEXT NOT NULL,
    renotify INTEGER NOT NULL, released INTEGER NOT NULL DEFAULT 0)""",
)


@dataclass(frozen=True, slots=True)
class ActiveMute:
    rule_id: str
    remaining_s: float | None


class Mutes:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def set(self, rule: str, boot: str, expiry: float | None, renotify: bool) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO mutes VALUES(?,?,?,?,0)", (rule, boot, expiry, renotify)
            )

    def remove(self, rule: str, boot: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO mutes VALUES(?,?,0,1,1)", (rule, boot))

    def active(self, boot: str, now: float) -> tuple[ActiveMute, ...]:
        return tuple(
            ActiveMute(rule, None if expiry is None else max(0, expiry - now))
            for rule, expiry in self.db.execute(
                "SELECT rule_id,expiry FROM mutes WHERE boot=? AND released=0"
                " AND (expiry IS NULL OR expiry>?)",
                (boot, now),
            )
        )

    def quiet(self, schedule: str, renotify: bool) -> None:
        if schedule != "off" and any(
            not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d", part)
            or part[:5] == part[6:]
            for part in schedule.split(",")
        ):
            raise ValueError("quiet hours require HH:MM-HH:MM (comma-separated) or off")
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO quiet_hours VALUES(1,?,?,?)",
                ("" if schedule == "off" else schedule, renotify, schedule == "off"),
            )
