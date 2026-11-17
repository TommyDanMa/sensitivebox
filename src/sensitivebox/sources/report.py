"""One read of every system source, for ``sensitivebox doctor``.

The daemon will poll the same readers but keep state (``NetMeter``, the previous ``CpuTimes``,
an ``NmWifiWatcher``); doctor is a one-shot, so it reports since-boot figures.
"""

import asyncio
import dataclasses
from dataclasses import dataclass
from pathlib import Path

from sensitivebox.hwmon.reader import Opener, default_opener
from sensitivebox.sources import apps, cpu, memory, net, psi, wifi
from sensitivebox.sources.bluez import BluetoothStatus, read_bluetooth
from sensitivebox.sources.dbus import NO_BUS, connect_system_bus
from sensitivebox.sources.wifi_nm import read_nm_wifi_once


@dataclass(frozen=True, slots=True)
class SourcesReport:
    meminfo: memory.MemInfo | None
    zram: tuple[memory.Zram, ...]
    vmstat: memory.VmStat | None
    pressure: dict[str, psi.Pressure]
    cpu_times: cpu.CpuTimes | None
    cpu_freqs: tuple[cpu.CoreFreq, ...]
    net: dict[str, net.NetCounters]
    wifi: wifi.WifiStatus
    apps: tuple[apps.AppMemory, ...] | None  # None: not sampled (memory isn't tight)
    apps_reason: str | None = None  # why apps is empty although memory is tight
    bluetooth: BluetoothStatus | None = None  # None: D-Bus not queried


def gather(proc_root: Path, sysfs_root: Path, opener: Opener = default_opener) -> SourcesReport:
    """Every file-based source. Wi-Fi comes from ``/proc/net/wireless`` only; see
    ``with_dbus`` for the NetworkManager fallback and Bluetooth."""
    meminfo = memory.read_meminfo(proc_root, opener)
    pressure = psi.read_all_pressure(proc_root, opener)
    top: tuple[apps.AppMemory, ...] | None = None
    reason: str | None = None
    if apps.should_sample_apps(meminfo, pressure.get("memory")):
        slice_dir = apps.app_slice_dir(proc_root, sysfs_root, opener)
        if slice_dir is None:
            top, reason = (), "no cgroup v2 app.slice for this user"
        else:
            top = apps.read_app_memory(slice_dir, opener=opener)
    return SourcesReport(
        meminfo=meminfo,
        zram=memory.read_zram(sysfs_root, opener),
        vmstat=memory.read_vmstat(proc_root, opener),
        pressure=pressure,
        cpu_times=cpu.read_cpu_times(proc_root, opener),
        cpu_freqs=cpu.read_cpu_freqs(sysfs_root, opener),
        net=net.read_net_counters(sysfs_root, opener=opener),
        wifi=wifi.choose(wifi.read_proc_wireless(proc_root, opener), None),
        apps=top,
        apps_reason=reason,
    )


def with_dbus(
    report: SourcesReport, bluetooth: BluetoothStatus, nm_wifi: wifi.WifiStatus
) -> SourcesReport:
    """Add Bluetooth, and NetworkManager's Wi-Fi signal unless ``/proc`` already had one."""
    wifi_status = report.wifi if report.wifi.available else nm_wifi
    return dataclasses.replace(report, bluetooth=bluetooth, wifi=wifi_status)


SNAPSHOT_TIMEOUT_S = 2.0


async def system_bus_snapshot() -> tuple[BluetoothStatus, wifi.WifiStatus]:
    """Read BlueZ and NetworkManager once, over one connection that is always closed."""
    bus = await connect_system_bus()
    if bus is None:
        return BluetoothStatus(reason=NO_BUS), wifi.WifiStatus.unavailable(NO_BUS)
    try:
        async with asyncio.timeout(SNAPSHOT_TIMEOUT_S):
            bt, nm = await asyncio.gather(read_bluetooth(bus), read_nm_wifi_once(bus))
    except TimeoutError:
        reason = "D-Bus timed out"
        return BluetoothStatus(reason=reason), wifi.WifiStatus.unavailable(reason)
    finally:
        bus.disconnect()
    return bt, nm
