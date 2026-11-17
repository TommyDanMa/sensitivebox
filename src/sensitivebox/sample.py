"""One fresh read of available sources for one-shot CLI commands."""

import math
import socket
from dataclasses import dataclass
from pathlib import Path

from sensitivebox import hwmon, sources
from sensitivebox.alerts import Missing
from sensitivebox.alerts.model import Value
from sensitivebox.fixtures import MachineRoot
from sensitivebox.hwmon import Chip, Reading, ReadStatus
from sensitivebox.hwmon.reader import default_opener, read_attr
from sensitivebox.simulate import fixture_opener
from sensitivebox.sources import SourcesReport
from sensitivebox.sources.power_supply import read_power_supplies


@dataclass(frozen=True, slots=True)
class Sample:
    host: str
    uptime: float | None
    chips: tuple[Chip, ...]
    readings: dict[str, Reading]
    sources: SourcesReport
    signals: dict[str, Value]


async def sample(sysfs_root: Path, proc_root: Path, root: MachineRoot | None = None) -> Sample:
    opener = fixture_opener if root is not None else default_opener
    try:
        chips = tuple(hwmon.discover(sysfs_root, opener=opener))
    except FileNotFoundError:
        chips = ()
    readings = hwmon.read_all(chips, opener)
    report = sources.gather(proc_root, sysfs_root, opener)
    bt, wifi = await (root.snapshot() if root is not None else sources.system_bus_snapshot())
    report = sources.with_dbus(report, bt, wifi)
    signals: dict[str, Value] = {
        name: r.value
        if r.status is ReadStatus.OK and r.value is not None
        else Missing(r.reason or r.status.value)
        for name, r in readings.items()
    }
    pressure = report.pressure.get("memory")
    signals["psi/memory/some_avg10"] = (
        pressure.some.avg10 if pressure else Missing("PSI unavailable")
    )
    signals["psi/memory/full_avg10"] = (
        pressure.full.avg10 if pressure and pressure.full else Missing("PSI unavailable")
    )
    mem = report.meminfo
    signals["memory/available_pct"] = (
        mem.available / mem.total * 100 if mem and mem.total else Missing("memory unavailable")
    )
    signals["vmstat/oom_kill_delta"] = Missing("one sample: no counter baseline")
    for supply in read_power_supplies(sysfs_root, opener):
        prefix = f"power_supply/{supply.name}"
        if supply.kind == "Battery":
            signals[f"{prefix}/capacity"] = (
                float(supply.capacity)
                if supply.capacity is not None
                else Missing("capacity unavailable")
            )
            signals[f"{prefix}/status"] = supply.status or Missing("status unavailable")
        if supply.is_usb_source:
            signals[f"{prefix}/power_w"] = (
                supply.power_w
                if supply.online is not False and supply.power_w is not None
                else Missing("port offline")
            )
    text = read_attr(proc_root / "uptime", opener)
    try:
        uptime = float(text.split()[0]) if text else None
        if uptime is not None and (not math.isfinite(uptime) or uptime < 0):
            uptime = None
    except (ValueError, IndexError):
        uptime = None
    host = (
        read_attr(proc_root / "sys/kernel/hostname", opener) or "fixture host"
        if root is not None
        else socket.gethostname()
    )
    return Sample(host.strip(), uptime, chips, readings, report, signals)
