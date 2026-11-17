"""Turns the sources into the signals the alert engine and the store consume, once per poll.

Polling is tiered (docs/research/new-relic-lessons.md §3). Cheap, fast-moving values are read on
every poll; slower ones are read less often and *held* in between, so the engine never sees a
false "no reading" just because a tier wasn't due:

====== =========================== ==========================================================
Tier   Interval                    Sources
====== =========================== ==========================================================
fast   the poll interval (1 s)     hwmon sensors, PSI
medium 2 s                         ``/proc/meminfo``, ``/proc/vmstat``, network counters
slow   5 s                         ``/sys/class/power_supply``
apps   at most every 20 s          per-app cgroup memory, and only while memory is tight
====== =========================== ==========================================================

A tier's interval is never shorter than the poll interval. Reads are plain small file reads: no
subprocess, no D-Bus call. Everything takes ``sysfs_root``/``proc_root`` so tests run on fixtures.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from sensitivebox import hwmon
from sensitivebox.alerts.engine import Clock
from sensitivebox.alerts.model import Missing, Value
from sensitivebox.hwmon.model import Limits, ReadStatus, Sensor
from sensitivebox.hwmon.reader import Opener, default_opener, read_int
from sensitivebox.sources.apps import AppMemory, app_slice_dir, read_app_memory, should_sample_apps
from sensitivebox.sources.cpu import CpuTimes, read_cpu_freqs, read_cpu_times, utilisation
from sensitivebox.sources.memory import MemInfo, read_meminfo, read_vmstat, read_zram
from sensitivebox.sources.net import NetMeter, read_net_counters
from sensitivebox.sources.power_supply import read_power_supplies
from sensitivebox.sources.psi import Pressure, read_pressure
from sensitivebox.sources.wifi import WifiSignal, read_proc_wireless

PSI_SOME = "psi/memory/some_avg10"
PSI_FULL = "psi/memory/full_avg10"
MEMORY_AVAILABLE_PCT = "memory/available_pct"
OOM_KILL_DELTA = "vmstat/oom_kill_delta"

APPS_TOP = 5


@dataclass(frozen=True, slots=True)
class Tiers:
    """Minimum seconds between reads of each tier (the poll interval is the fast tier)."""

    medium_s: float = 2.0
    slow_s: float = 5.0
    apps_s: float = 20.0

    def __post_init__(self) -> None:
        if min(self.medium_s, self.slow_s, self.apps_s) <= 0:
            raise ValueError("tier intervals must be positive")


@dataclass(frozen=True, slots=True)
class Snapshot:
    """What one poll produced."""

    at: float  # the collector's (monotonic) clock
    signals: Mapping[str, Value]  # what the alert engine evaluates
    metrics: Mapping[str, float] = field(default_factory=dict[str, float])  # stored, not alerted on
    apps: tuple[AppMemory, ...] = ()  # top apps by memory; empty unless memory is tight
    units: Mapping[str, str] = field(default_factory=dict[str, str])
    display: Mapping[str, Value] = field(default_factory=dict[str, Value])


class Collector:
    """Reads every source on its tier and returns a :class:`Snapshot` per call to ``collect``.

    ``collect`` is synchronous: a poll is a few dozen sub-millisecond file reads.
    """

    def __init__(
        self,
        sysfs_root: Path,
        proc_root: Path,
        *,
        clock: Clock,
        poll_interval: float = 1.0,
        tiers: Tiers = Tiers(),  # noqa: B008 - frozen, shared by design
        opener: Opener = default_opener,
        today: Callable[[], date] = date.today,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll interval must be positive")
        self._sysfs = sysfs_root
        self._proc = proc_root
        self._clock = clock
        self._poll = poll_interval
        self._tiers = tiers
        self._opener = opener
        self._today = today
        self._chips = hwmon.discover(sysfs_root, opener=opener)
        self.limits: dict[str, Limits] = {
            sensor.id: sensor.limits for chip in self._chips for sensor in chip.sensors
        }
        self._units = {sensor.id: sensor.unit for chip in self._chips for sensor in chip.sensors}
        self._units.update({PSI_SOME: "%", PSI_FULL: "%", MEMORY_AVAILABLE_PCT: "%"})
        self._apps_dir = app_slice_dir(proc_root, sysfs_root, opener)
        self._meter = NetMeter()
        self._held: dict[str, Value] = {}
        self._last_read: dict[str, float] = {}
        self._mem: MemInfo | None = None
        self._oom_last: int | None = None
        self._oom: Value = Missing("no baseline")
        self._apps: tuple[AppMemory, ...] = ()
        self._cpu: CpuTimes | None = None
        self._display: dict[str, Value] = {}
        self.wireless: dict[str, WifiSignal] = {}

    @property
    def sensors(self) -> tuple[Sensor, ...]:
        return tuple(sensor for chip in self._chips for sensor in chip.sensors)

    def reset_baselines(self) -> None:
        """After sleep: counters and held values are stale. Re-read every tier next poll and
        start rates and deltas from a fresh baseline (per-day network totals are kept)."""
        meter = NetMeter()
        meter.seed(self._meter.totals)
        self._meter = meter
        self._held.clear()
        self._last_read.clear()
        self._oom_last = None
        self._oom = Missing("no baseline")
        self._apps = ()
        self._cpu = None
        self._display.clear()

    def collect(self) -> Snapshot:
        now = self._clock()
        signals: dict[str, Value] = {}
        metrics: dict[str, float] = {}

        for sensor_id, reading in hwmon.read_all(self._chips, self._opener).items():
            if reading.status is ReadStatus.OK and reading.value is not None:
                signals[sensor_id] = reading.value
            else:
                signals[sensor_id] = Missing(reading.reason or reading.status.value)

        pressure = read_pressure(self._proc, "memory", self._opener)
        signals.update(_psi_signals(pressure))
        for resource in ("cpu", "io"):
            current = read_pressure(self._proc, resource, self._opener)
            for scope in ("some", "full"):
                line = getattr(current, scope) if current is not None else None
                for window in ("avg10", "avg60"):
                    identity = f"psi/{resource}/{scope}_{window}"
                    self._units[identity] = "%"
                    signals[identity] = (
                        getattr(line, window) if line is not None else Missing("PSI unavailable")
                    )

        if self._due("medium", self._tiers.medium_s, now):
            self._read_medium(now, metrics)
        if self._due("slow", self._tiers.slow_s, now):
            self._read_slow()
        signals.update(self._held)

        signals[OOM_KILL_DELTA] = self._oom
        if not isinstance(self._oom, Missing):
            self._oom = 0.0  # a delta is an event: published once, then back to zero

        self._read_apps(now, pressure)
        return Snapshot(now, signals, metrics, self._apps, dict(self._units), dict(self._display))

    def _due(self, tier: str, interval: float, now: float) -> bool:
        """True when ``interval`` has passed since the tier was last read, allowing half a poll
        of scheduling jitter so a 2 s tier on a 1 s poll doesn't slip to 3 s."""
        last = self._last_read.get(tier)
        if last is not None and now - last < max(interval, self._poll) - self._poll / 2:
            return False
        self._last_read[tier] = now
        return True

    def _read_medium(self, now: float, metrics: dict[str, float]) -> None:
        self._mem = read_meminfo(self._proc, self._opener)
        if self._mem is None or self._mem.total <= 0:
            self._held[MEMORY_AVAILABLE_PCT] = Missing("no MemAvailable in /proc/meminfo")
        else:
            self._held[MEMORY_AVAILABLE_PCT] = self._mem.available / self._mem.total * 100.0

        for name, attr in (
            ("total_bytes", "total"),
            ("available_bytes", "available"),
            ("used_bytes", "used"),
            ("swap_used_bytes", "swap_used"),
            ("swap_total_bytes", "swap_total"),
        ):
            self._hold(
                f"memory/{name}", float(getattr(self._mem, attr)) if self._mem else None, "B"
            )
        self._read_dashboard_system()

        self._read_oom()

        counters = read_net_counters(self._sysfs, opener=self._opener)
        self.wireless = read_proc_wireless(self._proc, self._opener)
        for identity in self._display:
            if identity.startswith("net/"):
                self._display[identity] = Missing("network interface unavailable")
        for iface, rate in self._meter.update(counters, monotonic=now, day=self._today()).items():
            for direction, value in (("rx", rate.rx_bps), ("tx", rate.tx_bps)):
                name = f"net/{iface}/{direction}_bps"
                self._units[name] = "B/s"
                self._display[name] = value if value is not None else Missing("no baseline")
            if rate.rx_bps is not None and rate.tx_bps is not None:
                metrics[f"net/{iface}/rx_bps"] = rate.rx_bps
                metrics[f"net/{iface}/tx_bps"] = rate.tx_bps
        if not counters:
            for direction in ("rx", "tx"):
                self._display[f"net/{direction}_bps"] = Missing("network interface unavailable")
                self._units[f"net/{direction}_bps"] = "B/s"
        # The existing store has no persistent daily totals yet. Never label session totals
        # as today's/month's bytes or invent zero. The dashboard still receives these entries.
        for period in ("today", "month"):
            for direction in ("rx", "tx"):
                name = f"net/{period}/{direction}_bytes"
                self._display[name] = Missing("persistent network totals unavailable")
                self._units[name] = "B"

    def _hold(self, identity: str, value: float | None, unit: str) -> None:
        self._held[identity] = value if value is not None else Missing("source unavailable")
        self._units[identity] = unit

    def _read_dashboard_system(self) -> None:
        current = read_cpu_times(self._proc, self._opener)
        util = utilisation(self._cpu, current) if self._cpu and current else None
        self._cpu = current
        self._hold("cpu/util_pct", util.overall * 100 if util else None, "%")
        for identity in self._held:
            if identity.startswith(("cpu/core", "zram/", "gpu/")):
                self._held[identity] = Missing("source unavailable")
        if util is not None:
            for cpu, share in zip(util.cpus, util.cores, strict=True):
                self._hold(f"cpu/core{cpu}/util_pct", share * 100, "%")
        for freq in read_cpu_freqs(self._sysfs, self._opener):
            self._hold(f"cpu/core{freq.cpu}/freq_hz", freq.cur_hz, "Hz")
            self.limits[f"cpu/core{freq.cpu}/freq_hz"] = Limits(min=freq.min_hz, max=freq.max_hz)
        self._hold("zram/ratio", None, "x")
        self._hold("zram/saved_bytes", None, "B")
        for zram in read_zram(self._sysfs, self._opener):
            self._hold(f"zram/{zram.name}/ratio", zram.ratio, "x")
            self._hold(f"zram/{zram.name}/saved_bytes", float(zram.saved_bytes), "B")
        self._hold("gpu/util_pct", None, "%")
        drm = self._sysfs / "class/drm"
        for path in sorted(drm.glob("card[0-9]*/device/gpu_busy_percent")):
            value = read_int(path, self._opener)
            self._hold(
                f"gpu/{path.parent.parent.name}/util_pct",
                float(value) if value is not None else None,
                "%",
            )

    def _read_oom(self) -> None:
        vmstat = read_vmstat(self._proc, self._opener)
        count = None if vmstat is None else vmstat.oom_kill
        if count is None:
            self._oom = Missing("no oom_kill counter in /proc/vmstat")
            self._oom_last = None
            return
        last, self._oom_last = self._oom_last, count
        if last is None:
            self._oom = Missing("no baseline")
        else:
            self._oom = float(max(count - last, 0))  # a counter that goes down is a new baseline

    def _read_slow(self) -> None:
        # Hold values between slow reads, but never across a read that lost the device.
        for signal in self._held:
            if signal.startswith("power_supply/"):
                self._held[signal] = Missing("power supply unavailable")
        for supply in read_power_supplies(self._sysfs, self._opener):
            prefix = f"power_supply/{supply.name}"
            if supply.kind == "Battery":
                self._units[f"{prefix}/capacity"] = "%"
                self._held[f"{prefix}/capacity"] = (
                    Missing("capacity unreadable") if supply.capacity is None else supply.capacity
                )
                self._held[f"{prefix}/status"] = (
                    Missing("status unreadable") if supply.status is None else supply.status
                )
            if supply.is_usb_source:
                self._units[f"{prefix}/power_w"] = "W"
                if supply.online is False:
                    self._held[f"{prefix}/power_w"] = Missing("port offline")
                elif supply.power_w is None:
                    self._held[f"{prefix}/power_w"] = Missing("no voltage or current")
                else:
                    self._held[f"{prefix}/power_w"] = supply.power_w

    def _read_apps(self, now: float, pressure: Pressure | None) -> None:
        if not should_sample_apps(self._mem, pressure):
            self._apps = ()
            self._last_read.pop("apps", None)  # memory eased: sample at once next time it's tight
            return
        if self._apps_dir is None or not self._apps_dir.is_dir():
            self._apps_dir = app_slice_dir(self._proc, self._sysfs, self._opener)
            self._apps = ()
            self._last_read.pop("apps", None)
        if self._apps_dir is None:
            return
        if self._due("apps", self._tiers.apps_s, now):
            self._apps = read_app_memory(self._apps_dir, top=APPS_TOP, opener=self._opener)


def _psi_signals(pressure: Pressure | None) -> dict[str, Value]:
    if pressure is None:
        reason = Missing("PSI unavailable (psi=0 or unsupported kernel)")
        return {PSI_SOME: reason, PSI_FULL: reason}
    full: Value = (
        Missing("no 'full' line in memory pressure")
        if pressure.full is None
        else pressure.full.avg10
    )
    return {PSI_SOME: pressure.some.avg10, PSI_FULL: full}
