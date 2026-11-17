"""Civil-time quiet hours and boot-scoped monotonic rule mutes."""

from datetime import datetime
from pathlib import Path

from sensitivebox.alerts.engine import Clock
from sensitivebox.alerts.routing import MuteDecision, Notification
from sensitivebox.store.mutes import Mutes
from sensitivebox.store.quality import local_zone


def boot_id(proc_root: Path) -> str:
    try:
        value = (proc_root / "sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        raise OSError("boot ID unavailable in selected proc root") from None
    if not value:
        raise OSError("empty boot ID in selected proc root")
    return value


def quiet_now(schedule: str, wall: float) -> bool:
    current = datetime.fromtimestamp(wall, local_zone()).strftime("%H:%M")
    for part in schedule.split(","):
        if not part:
            continue
        start, end = part.split("-")
        if (start <= current < end) if start < end else (current >= start or current < end):
            return True
    return False


class PersistentMute:
    def __init__(self, store: Mutes, boot: str, clock: Clock, wall: Clock) -> None:
        self.store, self.boot, self.clock, self.wall = store, boot, clock, wall
        self._now = clock()

    def decide(self, notification: Notification) -> MuteDecision:
        self._now = max(self._now, self.clock())
        row = self.store.db.execute(
            "SELECT expiry,renotify,released FROM mutes WHERE rule_id=? AND boot=?",
            (notification.rule_id, self.boot),
        ).fetchone()
        manual = "muted" if row and row[2] else None
        if row and not row[2] and (row[0] is None or row[0] > self._now):
            return MuteDecision(False, "muted", bool(row[1]))
        quiet = self.store.db.execute(
            "SELECT schedule,renotify,released FROM quiet_hours"
        ).fetchone()
        if quiet and notification.severity != "critical":
            if quiet_now(quiet[0], self.wall()):
                return MuteDecision(False, "quiet hours", bool(quiet[1]))
            if quiet[2] and manual is None:
                manual = "quiet hours"
        return MuteDecision(True, manual_release=manual)
