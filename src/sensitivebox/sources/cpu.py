"""CPU utilisation from ``/proc/stat`` and per-core clock speed from cpufreq.

Utilisation needs two samples; ``utilisation(prev, cur)`` is pure so the daemon owns the state.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from sensitivebox.hwmon.reader import Opener, default_opener, read_attr, read_int

_CPU_DIR_RE = re.compile(r"^cpu(\d+)$")
_KHZ = 1000.0


_FIELDS = 8  # user nice system idle iowait irq softirq steal; guest time is already in user
_IDLE, _IOWAIT = 3, 4


@dataclass(frozen=True, slots=True)
class CoreTimes:
    """Jiffies since boot, per field: user nice system idle iowait irq softirq steal. Kernels
    that report fewer fields have the rest as 0. ``busy`` excludes idle and iowait."""

    fields: tuple[int, ...]

    @property
    def total(self) -> int:
        return sum(self.fields)

    @property
    def busy(self) -> int:
        return self.total - self.fields[_IDLE] - self.fields[_IOWAIT]


@dataclass(frozen=True, slots=True)
class CpuTimes:
    overall: CoreTimes
    cores: tuple[CoreTimes, ...]
    cpus: tuple[int, ...] = ()  # the CPU number of each entry in ``cores``

    @property
    def busy_since_boot(self) -> float:
        return self.overall.busy / self.overall.total if self.overall.total else 0.0


@dataclass(frozen=True, slots=True)
class CpuUtil:
    """Share of time busy between two samples, 0.0 to 1.0."""

    overall: float
    cores: tuple[float, ...]
    cpus: tuple[int, ...] = ()  # the CPU number of each entry in ``cores``, as in ``CoreFreq``


def _core_times(fields: list[str]) -> CoreTimes | None:
    if len(fields) < 4 or not all(f.isdigit() for f in fields):
        return None
    values = [int(f) for f in fields[:_FIELDS]]
    return CoreTimes(tuple(values + [0] * (_FIELDS - len(values))))


def parse_stat(text: str) -> CpuTimes | None:
    overall: CoreTimes | None = None
    cores: list[tuple[int, CoreTimes]] = []
    for line in text.splitlines():
        if not line.startswith("cpu"):
            continue
        label, *fields = line.split()
        times = _core_times(fields)
        if times is None:
            continue
        if label == "cpu":
            overall = times
        elif label[3:].isdigit():
            cores.append((int(label[3:]), times))
    if overall is None:
        return None
    cores.sort()
    return CpuTimes(overall, tuple(t for _, t in cores), tuple(n for n, _ in cores))


def read_cpu_times(proc_root: Path, opener: Opener = default_opener) -> CpuTimes | None:
    text = read_attr(proc_root / "stat", opener)
    return parse_stat(text) if text else None


def _share(prev: CoreTimes, cur: CoreTimes) -> float | None:
    """Busy share between two samples, or ``None`` when the counters were reset.

    Each field's change is taken on its own and a field that went down counts as 0: the kernel
    documents that iowait can decrease, and letting it subtract from the total would push the
    share past 100 % (ADV-009).
    """
    if cur.total < prev.total:
        return None  # a reset (or a different machine), not a measurement
    deltas = [max(c - p, 0) for p, c in zip(prev.fields, cur.fields, strict=True)]
    total = sum(deltas)
    busy = total - deltas[_IDLE] - deltas[_IOWAIT]
    return busy / total if total else 0.0


def utilisation(prev: CpuTimes, cur: CpuTimes) -> CpuUtil | None:
    """``None`` if the counters were reset or the set of online CPUs changed in between: a core
    is only ever compared with itself (ADV-010)."""
    overall = _share(prev.overall, cur.overall)
    if overall is None or prev.cpus != cur.cpus or len(prev.cores) != len(cur.cores):
        return None
    cores: list[float] = []
    for p, c in zip(prev.cores, cur.cores, strict=True):
        share = _share(p, c)
        if share is None:
            return None
        cores.append(share)
    return CpuUtil(overall, tuple(cores), cur.cpus)


@dataclass(frozen=True, slots=True)
class CoreFreq:
    """Clock speed in Hz. ``None`` where cpufreq doesn't say (no driver, offline core)."""

    cpu: int
    cur_hz: float | None
    min_hz: float | None
    max_hz: float | None


def _khz(path: Path, opener: Opener) -> float | None:
    value = read_int(path, opener)
    return value * _KHZ if value is not None else None


def read_cpu_freqs(sysfs_root: Path, opener: Opener = default_opener) -> tuple[CoreFreq, ...]:
    """One entry per ``devices/system/cpu/cpuN``, in CPU order. Empty without that tree."""
    base = sysfs_root / "devices" / "system" / "cpu"
    try:
        entries = list(base.iterdir())
    except OSError:
        return ()
    cpus = sorted(int(m[1]) for e in entries if (m := _CPU_DIR_RE.match(e.name)))
    freqs: list[CoreFreq] = []
    for n in cpus:
        cpufreq = base / f"cpu{n}" / "cpufreq"
        freqs.append(
            CoreFreq(
                cpu=n,
                cur_hz=_khz(cpufreq / "scaling_cur_freq", opener),
                min_hz=_khz(cpufreq / "cpuinfo_min_freq", opener),
                max_hz=_khz(cpufreq / "cpuinfo_max_freq", opener),
            )
        )
    return tuple(freqs)
