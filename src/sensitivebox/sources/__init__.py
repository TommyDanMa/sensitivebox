"""System sources beyond hwmon: memory, pressure, CPU, network, Wi-Fi and Bluetooth. Read-only.

File readers take ``proc_root``/``sysfs_root`` so tests run against fixtures. A source the box
doesn't have returns ``None`` or an empty collection; it never raises.
"""

from sensitivebox.sources.apps import (
    AppMemory,
    app_name,
    app_slice_dir,
    read_app_memory,
    should_sample_apps,
)
from sensitivebox.sources.bluez import BluetoothStatus, BtAdapter, BtDevice, read_bluetooth
from sensitivebox.sources.bluez_watch import BluezWatcher
from sensitivebox.sources.cpu import (
    CoreFreq,
    CpuTimes,
    CpuUtil,
    read_cpu_freqs,
    read_cpu_times,
    utilisation,
)
from sensitivebox.sources.memory import MemInfo, VmStat, Zram, read_meminfo, read_vmstat, read_zram
from sensitivebox.sources.net import (
    DEFAULT_SKIP,
    DayTotal,
    NetCounters,
    NetMeter,
    NetRate,
    read_net_counters,
)
from sensitivebox.sources.power_supply import PowerSupply, read_power_supplies
from sensitivebox.sources.psi import Pressure, PsiLine, read_all_pressure, read_pressure
from sensitivebox.sources.report import SourcesReport, gather, system_bus_snapshot, with_dbus
from sensitivebox.sources.wifi import WifiSignal, WifiStatus, read_proc_wireless
from sensitivebox.sources.wifi_nm import NmWifiWatcher, read_nm_wifi_once

__all__ = [
    "DEFAULT_SKIP",
    "AppMemory",
    "BluetoothStatus",
    "BluezWatcher",
    "BtAdapter",
    "BtDevice",
    "CoreFreq",
    "CpuTimes",
    "CpuUtil",
    "DayTotal",
    "MemInfo",
    "NetCounters",
    "NetMeter",
    "NetRate",
    "NmWifiWatcher",
    "PowerSupply",
    "Pressure",
    "PsiLine",
    "SourcesReport",
    "VmStat",
    "WifiSignal",
    "WifiStatus",
    "Zram",
    "app_name",
    "app_slice_dir",
    "gather",
    "read_all_pressure",
    "read_app_memory",
    "read_bluetooth",
    "read_cpu_freqs",
    "read_cpu_times",
    "read_meminfo",
    "read_net_counters",
    "read_nm_wifi_once",
    "read_power_supplies",
    "read_pressure",
    "read_proc_wireless",
    "read_vmstat",
    "read_zram",
    "should_sample_apps",
    "system_bus_snapshot",
    "utilisation",
    "with_dbus",
]
