"""Memory per app, from the user's cgroup v2 ``app.slice``.

GNOME (and any desktop using systemd's XDG integration) launches each app in its own scope
or service under ``user@UID.service/app.slice``, so one ``memory.current`` read per app gives
its memory, child processes included. That still means dozens of reads, so it happens only
when memory is tight (see ``should_sample_apps``), never as a per-process scan.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

from sensitivebox.hwmon.reader import Opener, default_opener, read_attr, read_int
from sensitivebox.sources.memory import MemInfo
from sensitivebox.sources.psi import Pressure

USED_THRESHOLD = 0.80  # sample when more than this share of memory is in use
PSI_HIGH = 10.0  # ... or memory "some" avg10 is at least this (%), or it is rising

_USER_SLICE_RE = re.compile(r"/user-(\d+)\.slice(?:/|$)")
_ESCAPE_RE = re.compile(r"\\x([0-9a-fA-F]{2})")
_SPAWN_RE = re.compile(r"^(?P<terminal>[^-]+)-spawn-[0-9a-fA-F-]+$")
# snapd tracks each app instance as snap.<snap>.<app>-<uuid>.scope (services drop the uuid).
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_SNAP_RE = re.compile(rf"^snap\.(?P<snap>[^.]+)\.(?P<app>.+?)(?:-{_UUID})?$")
_UNIT_SUFFIXES = (".scope", ".service", ".slice")


@dataclass(frozen=True, slots=True)
class AppMemory:
    name: str
    bytes: int
    units: int = 1  # cgroups summed into this entry (same app name)


def app_slice_dir(
    proc_root: Path,
    sysfs_root: Path,
    opener: Opener = default_opener,
    *,
    fallback_uid: int | None = None,
) -> Path | None:
    """The calling user's ``app.slice`` directory, or ``None`` without cgroup v2 / systemd.

    The uid comes from ``<proc_root>/self/cgroup`` (so fixtures carry their own), falling back
    to ``fallback_uid`` and then the real uid.
    """
    uid: int | None = None
    text = read_attr(proc_root / "self" / "cgroup", opener) or ""
    for line in text.splitlines():
        if line.startswith("0::") and (m := _USER_SLICE_RE.search(line[3:])):
            uid = int(m[1])
            break
    if uid is None:
        uid = fallback_uid if fallback_uid is not None else os.getuid()
    path = (
        sysfs_root
        / "fs"
        / "cgroup"
        / "user.slice"
        / f"user-{uid}.slice"
        / f"user@{uid}.service"
        / "app.slice"
    )
    return path if path.is_dir() else None


def _unescape(text: str) -> str:
    return _ESCAPE_RE.sub(lambda m: chr(int(m[1], 16)), text)


def app_name(unit: str) -> str:
    """A readable app name from a systemd unit name.

    Follows the XDG naming scheme ``app[-<launcher>]-<app id>[-<random>].scope`` and
    ``app[-<launcher>]-<app id>[@<random>].service``, and snapd's
    ``snap.<snap>.<app>[-<uuid>].scope``. A literal ``-`` inside a part is
    escaped as ``\\x2d``, so splitting on raw ``-`` *before* unescaping is unambiguous.
    """
    stem, kind = unit, ""
    for suffix in _UNIT_SUFFIXES:
        if unit.endswith(suffix):
            stem, kind = unit[: -len(suffix)], suffix
            break
    if kind == ".service":
        stem = stem.split("@", 1)[0]
    parts = stem.split("-")
    if parts[0] == "app" and len(parts) >= 2:
        rest = parts[1:]
        if kind == ".scope" and len(rest) >= 2:
            rest = rest[:-1]  # the random/PID suffix
        if len(rest) >= 2:
            rest = rest[1:]  # the launcher, e.g. "gnome" or "flatpak"
        return _unescape("-".join(rest))
    if m := _SPAWN_RE.match(stem):
        return f"terminal ({_unescape(m['terminal'])})"
    if m := _SNAP_RE.match(stem):  # one name for every instance of a snap app (ADV-019)
        snap, app = _unescape(m["snap"]), _unescape(m["app"])
        return snap if app == snap else f"{snap} ({app})"
    return _unescape(stem)


def read_app_memory(
    slice_dir: Path, *, top: int = 5, opener: Opener = default_opener
) -> tuple[AppMemory, ...]:
    """The ``top`` apps by ``memory.current``, largest first. Same-named units are summed."""
    try:
        children = sorted(p for p in slice_dir.iterdir() if p.is_dir())
    except OSError:
        return ()
    totals: dict[str, AppMemory] = {}
    for child in children:
        current = read_int(child / "memory.current", opener)
        if current is None:
            continue
        name = app_name(child.name)
        prev = totals.get(name)
        totals[name] = (
            AppMemory(name, current)
            if prev is None
            else AppMemory(name, prev.bytes + current, prev.units + 1)
        )
    ranked = sorted(totals.values(), key=lambda a: (-a.bytes, a.name))
    return tuple(ranked[:top])


def should_sample_apps(mem: MemInfo | None, memory_pressure: Pressure | None) -> bool:
    """Whether memory is tight enough to be worth listing the top apps."""
    if mem is not None and mem.used_fraction > USED_THRESHOLD:
        return True
    if memory_pressure is None:
        return False
    some = memory_pressure.some
    return some.avg10 >= PSI_HIGH or some.rising
