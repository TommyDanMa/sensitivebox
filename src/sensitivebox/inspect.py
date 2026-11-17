"""Public diagnostic models omit Bluetooth aliases before serialization."""

from dataclasses import dataclass
from pathlib import Path

from sensitivebox import __version__, alerts, doctor
from sensitivebox.hwmon import Group, Kind, Limits, Reading
from sensitivebox.sample import Sample
from sensitivebox.sources import SourcesReport
from sensitivebox.sources.apps import AppMemory
from sensitivebox.sources.cpu import CoreFreq, CpuTimes
from sensitivebox.sources.memory import VmStat, Zram
from sensitivebox.sources.net import NetCounters
from sensitivebox.sources.psi import Pressure
from sensitivebox.sources.wifi import WifiStatus


@dataclass(frozen=True, slots=True)
class MemoryView:
    total: int
    available: int
    used: int
    used_pct: float
    cached: int
    dirty: int
    swap_total: int
    swap_used: int


@dataclass(frozen=True, slots=True)
class AdapterView:
    name: str
    powered: bool


@dataclass(frozen=True, slots=True)
class DeviceView:
    kind: str
    battery: int | None
    adapter: str


@dataclass(frozen=True, slots=True)
class BluetoothView:
    adapters: tuple[AdapterView, ...]
    devices: tuple[DeviceView, ...]
    reason: str | None


@dataclass(frozen=True, slots=True)
class SourcesView:
    memory: MemoryView | None
    pressure: dict[str, Pressure]
    apps: tuple[AppMemory, ...] | None
    apps_reason: str | None
    zram: tuple[Zram, ...]
    vmstat: VmStat | None
    cpu_times: CpuTimes | None
    cpu_freqs: tuple[CoreFreq, ...]
    network: dict[str, NetCounters]
    wifi: WifiStatus
    bluetooth: BluetoothView


def public_sources(report: SourcesReport) -> SourcesView:
    mem = report.meminfo
    memory = (
        MemoryView(
            mem.total,
            mem.available,
            mem.used,
            mem.used_fraction * 100,
            mem.cached,
            mem.dirty,
            mem.swap_total,
            mem.swap_used,
        )
        if mem
        else None
    )
    bt = report.bluetooth
    bluetooth = BluetoothView(
        tuple(AdapterView(a.name, a.powered) for a in bt.adapters) if bt else (),
        tuple(DeviceView(d.icon or "device", d.battery, d.adapter) for d in bt.devices)
        if bt
        else (),
        bt.reason if bt else "not queried",
    )
    return SourcesView(
        memory,
        report.pressure,
        report.apps,
        report.apps_reason,
        report.zram,
        report.vmstat,
        report.cpu_times,
        report.cpu_freqs,
        report.net,
        report.wifi,
        bluetooth,
    )


@dataclass(frozen=True, slots=True)
class DoctorSensor:
    id: str
    label: str
    group: Group
    kind: Kind
    unit: str
    limits: Limits
    dropped_limits: tuple[tuple[str, float], ...]
    reading: Reading
    default_rules: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EmptyChip:
    name: str
    bus_id: str
    path: str


@dataclass(frozen=True, slots=True)
class DoctorView:
    package_version: str
    sensors: tuple[DoctorSensor, ...]
    empty_chips: tuple[EmptyChip, ...]
    sources: SourcesView
    text: str


def doctor_view(sample: Sample, sysfs_root: Path, proc_root: Path, verbose: bool) -> DoctorView:
    rules = alerts.load_defaults()
    sensors = tuple(
        DoctorSensor(
            s.id,
            s.label,
            s.group,
            s.kind,
            s.unit,
            s.limits,
            s.dropped_limits,
            sample.readings[s.id],
            tuple(r.id for r in rules if r.matches(s.id)),
        )
        for c in sample.chips
        for s in c.sensors
    )
    empty = tuple(EmptyChip(c.name, c.bus_id, str(c.path)) for c in sample.chips if not c.sensors)
    text = (
        f"Sensitive Box {__version__} · sysfs root {sysfs_root} · proc root {proc_root}\n\n"
        + doctor.render(sample.chips, sample.readings, verbose=verbose, rules=rules)
        + "\n\n"
        + doctor.render_sources(sample.sources)
    )
    return DoctorView(__version__, sensors, empty, public_sources(sample.sources), text)


@dataclass(frozen=True, slots=True)
class AppsView:
    sources: SourcesView


def render_apps(report: SourcesReport) -> str:
    pressure = report.pressure.get("memory")
    psi = f"{pressure.some.avg10:.2f} % some" if pressure else "unavailable"
    lines = [f"Memory PSI {psi}"]
    if report.apps is None:
        lines.append("Apps not sampled: memory is healthy and pressure is steady")
    elif not report.apps:
        lines.append(report.apps_reason or "No apps found in this user's cgroup app.slice")
    else:
        lines.extend(f"{app.name}  {doctor.format_bytes(app.bytes)}" for app in report.apps)
    return "\n".join(lines)
