"""CLI writes for the SQLite mute control plane."""

import time
from dataclasses import dataclass
from pathlib import Path

from sensitivebox.alerts.engine import Clock
from sensitivebox.alerts.model import parse_duration
from sensitivebox.alerts.mute import boot_id
from sensitivebox.store import Store
from sensitivebox.store.mutes import ActiveMute


@dataclass(frozen=True, slots=True)
class MuteView:
    mutes: tuple[ActiveMute, ...]
    message: str


def control(
    path: Path,
    proc_root: Path,
    command: str,
    rule: str,
    duration: str,
    renotify: bool,
    *,
    clock: Clock = time.monotonic,
) -> MuteView:
    expiry: float | None = None
    if command == "mute" and duration != "until-reboot":
        seconds = parse_duration(duration)
        if seconds <= 0:
            raise ValueError("mute duration must be positive; use e.g. 30m or until-reboot")
        expiry = clock() + seconds
    boot = boot_id(proc_root) if command != "quiet" else ""
    with Store(path, clock=time.time, start_session=False) as store:
        if command == "mute":
            store.mutes.set(rule, boot, expiry, renotify)
        elif command == "unmute":
            store.mutes.remove(rule, boot)
        else:
            store.mutes.quiet(duration, renotify)
        return MuteView(store.mutes.active(boot, clock()), f"{command}: {rule or duration}")
