"""Memory: ``/proc/meminfo``, zram ``mm_stat`` and the ``/proc/vmstat`` counters we care about.

Used memory is always ``MemTotal - MemAvailable``. "Free" memory leaves out reclaimable page
cache, so it makes every Linux box look full.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from sensitivebox.hwmon.reader import Opener, default_opener, read_attr

_KB = 1024
_ZRAM_RE = re.compile(r"^zram(\d+)$")


@dataclass(frozen=True, slots=True)
class MemInfo:
    """``/proc/meminfo`` in bytes."""

    total: int
    available: int
    cached: int = 0
    dirty: int = 0
    swap_total: int = 0
    swap_free: int = 0

    @property
    def used(self) -> int:
        return max(self.total - self.available, 0)

    @property
    def used_fraction(self) -> float:
        return self.used / self.total if self.total else 0.0

    @property
    def swap_used(self) -> int:
        return max(self.swap_total - self.swap_free, 0)


def parse_meminfo(text: str) -> MemInfo | None:
    """``None`` without MemTotal or MemAvailable (kernels before 3.14 lack the latter)."""
    fields: dict[str, int] = {}
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        parts = rest.split()
        if not sep or not parts or not parts[0].isdigit():
            continue
        scale = _KB if parts[1:2] == ["kB"] else 1
        fields[key.strip()] = int(parts[0]) * scale
    if "MemTotal" not in fields or "MemAvailable" not in fields:
        return None
    return MemInfo(
        total=fields["MemTotal"],
        available=fields["MemAvailable"],
        cached=fields.get("Cached", 0),
        dirty=fields.get("Dirty", 0),
        swap_total=fields.get("SwapTotal", 0),
        swap_free=fields.get("SwapFree", 0),
    )


def read_meminfo(proc_root: Path, opener: Opener = default_opener) -> MemInfo | None:
    text = read_attr(proc_root / "meminfo", opener)
    return parse_meminfo(text) if text else None


@dataclass(frozen=True, slots=True)
class Zram:
    """One zram device, from the first three ``mm_stat`` fields (bytes)."""

    name: str
    orig_bytes: int  # uncompressed data stored
    compr_bytes: int  # compressed size of that data
    mem_used_bytes: int  # RAM actually used, including allocator overhead

    @property
    def ratio(self) -> float | None:
        """Compression ratio, e.g. 3.1; ``None`` while the device holds nothing."""
        return self.orig_bytes / self.compr_bytes if self.compr_bytes else None

    @property
    def saved_bytes(self) -> int:
        return max(self.orig_bytes - self.mem_used_bytes, 0)


def read_zram(sysfs_root: Path, opener: Opener = default_opener) -> tuple[Zram, ...]:
    """Every ``block/zramN`` with a readable ``mm_stat``, in device order. Empty without zram."""
    block = sysfs_root / "block"
    try:
        entries = list(block.iterdir())
    except OSError:
        return ()
    devices: list[tuple[int, Zram]] = []
    for entry in entries:
        m = _ZRAM_RE.match(entry.name)
        if not m:
            continue
        text = read_attr(entry / "mm_stat", opener)
        fields = text.split() if text else []
        if len(fields) < 3 or not all(f.isdigit() for f in fields[:3]):
            continue
        orig, compr, used = (int(f) for f in fields[:3])
        devices.append((int(m[1]), Zram(entry.name, orig, compr, used)))
    return tuple(zram for _, zram in sorted(devices))


@dataclass(frozen=True, slots=True)
class VmStat:
    """Cumulative since boot. ``None`` for a counter this kernel doesn't have."""

    oom_kill: int | None
    pswpin: int | None  # pages swapped in
    pswpout: int | None  # pages swapped out


def read_vmstat(proc_root: Path, opener: Opener = default_opener) -> VmStat | None:
    text = read_attr(proc_root / "vmstat", opener)
    if text is None:
        return None
    wanted = {"oom_kill", "pswpin", "pswpout"}
    values: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in wanted and parts[1].isdigit():
            values[parts[0]] = int(parts[1])
    return VmStat(values.get("oom_kill"), values.get("pswpin"), values.get("pswpout"))
