"""Allowlisted, private diagnostic exports and self-contained machine fixtures."""

import io
import os
import re
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from sensitivebox import __version__, sources
from sensitivebox.fixtures import MachineRoot, Properties
from sensitivebox.history import log_view
from sensitivebox.hwmon.ids import bus_id
from sensitivebox.inspect import doctor_view
from sensitivebox.output import Document, encode
from sensitivebox.redact import Redactor
from sensitivebox.sample import sample
from sensitivebox.simulate import fixture_opener
from sensitivebox.sources.apps import app_name, should_sample_apps
from sensitivebox.sources.bluez import ADAPTER, BATTERY, DEVICE, BluetoothStatus
from sensitivebox.sources.memory import read_meminfo
from sensitivebox.sources.psi import read_all_pressure
from sensitivebox.sources.wifi import WifiStatus
from sensitivebox.sources.wifi_nm import ACCESS_POINT, WIRELESS
from sensitivebox.sources.wifi_nm import DEVICE as NM_DEVICE
from sensitivebox.store.events import Events
from sensitivebox.store.read import reader

MAX_FILE = 65536
MAX_MEMBERS = 4096
MAX_BYTES = 16 * 1024 * 1024
ATTR = re.compile(
    r"(?:(?:temp|fan|in|curr|power|energy|humidity|freq)\d+_(?:input|average|label|min|max|crit|alarm)|pwm\d+)$"
)
PROC_FILES = (
    "meminfo",
    "vmstat",
    "stat",
    "uptime",
    "pressure/memory",
    "pressure/cpu",
    "pressure/io",
    "net/wireless",
    "sys/kernel/osrelease",
)
POWER_FILES = (
    "type",
    "status",
    "capacity",
    "online",
    "voltage_now",
    "current_now",
    "energy_now",
    "energy_full",
    "energy_full_design",
    "power_now",
)


@dataclass(frozen=True, slots=True)
class Entry:
    name: str
    data: str = ""
    link: str | None = None
    directory: bool = False


@dataclass(frozen=True, slots=True)
class ReportView:
    output: str
    fixture: bool
    members: int
    omissions: tuple[str, ...]


class Bundle:
    def __init__(self) -> None:
        self.entries: dict[str, Entry] = {}
        self.omissions: list[str] = []
        self.bytes = 0

    def add(
        self, name: str, data: str = "", *, link: str | None = None, directory: bool = False
    ) -> None:
        parts = PurePosixPath(name).parts
        if not parts or name.startswith("/") or ".." in parts:
            raise ValueError("unsafe report member")
        if link and (PurePosixPath(link).is_absolute() or not _safe_link(name, link)):
            raise ValueError("unsafe report link")
        if len(self.entries) >= MAX_MEMBERS or self.bytes + len(data.encode()) > MAX_BYTES:
            raise OSError("report exceeded bounded collection budget")
        self.entries[name] = Entry(name, data, link, directory)
        self.bytes += len(data.encode())
        for parent in PurePosixPath(name).parents:
            if str(parent) != ".":
                self.entries.setdefault(str(parent), Entry(str(parent), directory=True))

    def copy(self, source: Path, name: str, *, root: Path) -> None:
        try:
            if not source.resolve().is_relative_to(root.resolve()):
                self.omissions.append(f"{name}: link escapes selected root")
                return
            with source.open() as stream:
                data = stream.read(MAX_FILE + 1)
            if len(data) > MAX_FILE:
                self.omissions.append(f"{name}: exceeds file size limit")
                return
        except OSError:
            return  # absent/unreadable files stay absent, never filled from the host
        self.add(name, data)


def _safe_link(name: str, target: str) -> bool:
    depth = len(PurePosixPath(name).parent.parts)
    for part in PurePosixPath(target).parts:
        depth += -1 if part == ".." else 0 if part == "." else 1
        if depth < 0:
            return False
    return True


def _children(path: Path) -> list[Path]:
    try:
        return sorted(path.iterdir())[:MAX_MEMBERS]
    except OSError:
        return []


def _files(bundle: Bundle, sysfs_root: Path, proc_root: Path, keep_apps: bool) -> list[str]:
    private: list[str] = []
    for part in ("sys", "proc", "dbus", "sys/class/hwmon"):
        bundle.add(part, directory=True)
    for index, chip in enumerate(_children(sysfs_root / "class/hwmon")):
        prefix = f"sys/class/hwmon/hwmon{index}"
        bundle.add(prefix, directory=True)
        for attr in _children(chip):
            if attr.name == "name" or ATTR.fullmatch(attr.name):
                bundle.copy(attr, f"{prefix}/{attr.name}", root=sysfs_root)
        if (chip / "device").is_symlink():
            bus = bus_id(chip)
            destination = f"sys/devices/{bus}"
            bundle.add(destination, directory=True)
            bundle.add(f"{prefix}/device", link=os.path.relpath(destination, prefix))
    for relative in PROC_FILES:
        bundle.copy(proc_root / relative, "proc/" + relative, root=proc_root)
    for supply in _children(sysfs_root / "class/power_supply"):
        for name in POWER_FILES:
            bundle.copy(
                supply / name, f"sys/class/power_supply/{supply.name}/{name}", root=sysfs_root
            )
    for device in _children(sysfs_root / "class/net"):
        for name in ("statistics/rx_bytes", "statistics/tx_bytes", "operstate"):
            bundle.copy(device / name, f"sys/class/net/{device.name}/{name}", root=sysfs_root)
    for device in _children(sysfs_root / "block"):
        if re.fullmatch(r"zram\d+", device.name):
            bundle.copy(device / "mm_stat", f"sys/block/{device.name}/mm_stat", root=sysfs_root)
    for cpu in _children(sysfs_root / "devices/system/cpu"):
        if re.fullmatch(r"cpu\d+", cpu.name):
            for name in ("scaling_cur_freq", "cpuinfo_min_freq", "cpuinfo_max_freq"):
                relative = f"devices/system/cpu/{cpu.name}/cpufreq/{name}"
                bundle.copy(sysfs_root / relative, "sys/" + relative, root=sysfs_root)
    bundle.copy(
        sysfs_root / "class/dmi/id/bios_version", "sys/class/dmi/id/bios_version", root=sysfs_root
    )
    # Only the selected user's app.slice. The exported UID is synthetic and needs no host UID.
    try:
        cgroup = (proc_root / "self/cgroup").read_text()[:MAX_FILE]
    except OSError:
        cgroup = ""
    match = re.search(r"/user-(\d+)\.slice(?:/|$)", cgroup)
    if match and should_sample_apps(
        read_meminfo(proc_root, fixture_opener),
        read_all_pressure(proc_root, fixture_opener).get("memory"),
    ):
        uid = match[1]
        relative = f"fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/app.slice"
        target = "sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice"
        bundle.add(
            "proc/self/cgroup", "0::/user.slice/user-1000.slice/user@1000.service/app.slice\n"
        )
        for index, app in enumerate(_children(sysfs_root / relative)):
            name = app.name if keep_apps else f"app-report-app{index}-1.scope"
            if not keep_apps:
                private.extend((app.name, app_name(app.name)))
            bundle.copy(app / "memory.current", f"{target}/{name}/memory.current", root=sysfs_root)
    return private


def _dbus(bundle: Bundle, bt: BluetoothStatus, wifi: WifiStatus) -> list[str]:
    objects: Properties = {}
    private = [a.alias for a in bt.adapters] + [d.alias for d in bt.devices]
    for index, adapter in enumerate(bt.adapters):
        path = f"/org/bluez/hci{index}"
        objects[path] = {ADAPTER: {"Powered": adapter.powered}}
        for number, device in enumerate(d for d in bt.devices if d.adapter == adapter.name):
            device_path = f"{path}/device{number}"
            props: dict[str, str | bool | int] = {"Connected": True, "Adapter": path}
            if device.icon:
                props["Icon"] = device.icon
            objects[device_path] = {DEVICE: props}
            if device.battery is not None:
                objects[device_path][BATTERY] = {"Percentage": device.battery}
    if bt.available:
        bundle.add("dbus/bluez-managed-objects.json", encode(objects))
    if wifi.signal:
        signal = wifi.signal
        ap = "/org/freedesktop/NetworkManager/AccessPoint/1"
        device = "/org/freedesktop/NetworkManager/Devices/1"
        network: dict[str, Properties] = {
            "devices": {
                device: {
                    NM_DEVICE: {"DeviceType": 2, "Interface": signal.iface},
                    WIRELESS: {"ActiveAccessPoint": ap},
                }
            },
            "access_points": {ap: {ACCESS_POINT: {"Strength": signal.percent}}},
        }
        bundle.add("dbus/nm-wifi.json", encode(network))
    return private


def _sanitize(bundle: Bundle, private: list[str]) -> Bundle:
    clean = Bundle()
    policy = Redactor(tuple(sorted({p for p in private if p}, key=len, reverse=True)))
    for entry in bundle.entries.values():
        clean.add(
            policy(entry.name),
            policy(entry.data),
            link=policy(entry.link) if entry.link else None,
            directory=entry.directory,
        )
    clean.omissions = [policy(line) for line in bundle.omissions]
    return clean


def _write_machine(bundle: Bundle, root: Path) -> None:
    for entry in sorted(bundle.entries.values(), key=lambda e: (not e.directory, e.name)):
        path = root / entry.name
        path.parent.mkdir(parents=True, exist_ok=True)
        if entry.directory:
            path.mkdir(exist_ok=True)
        elif entry.link is not None:
            path.symlink_to(entry.link)
        else:
            path.write_text(entry.data)


async def export(
    output: Path,
    sysfs_root: Path,
    proc_root: Path,
    *,
    root: MachineRoot | None = None,
    fixture: bool = False,
    db: Path | None = None,
    keep_app_names: bool = False,
) -> ReportView:
    # Exclusive creation precedes collection, so an existing report is never touched.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        bundle = Bundle()
        private = _files(bundle, sysfs_root, proc_root, keep_app_names)
        bt, wifi = await (root.snapshot() if root is not None else sources.system_bus_snapshot())
        private.extend(_dbus(bundle, bt, wifi))
        bundle = _sanitize(bundle, private)
        with TemporaryDirectory(prefix="sensitivebox-report-") as temporary:
            machine_path = Path(temporary)
            _write_machine(bundle, machine_path)
            machine = MachineRoot.load(machine_path)
            current = await sample(machine.sys, machine.proc, machine)
            view = doctor_view(current, machine.sys, machine.proc, False)
            # Diagnostic output uses synthetic relative roots, never collector paths.
            diagnostic = encode(
                Document("doctor", view), redactor=Redactor(tuple(private))
            ).replace(str(machine_path), ".")
            bundle.add("diagnostics.json", Redactor(tuple(private))(diagnostic))
        if db is not None:
            history = log_view(db, 0, 100)
            with reader(db) as connection:
                weekly = Events(connection, time.time, writer=False).weekly() if connection else []
                markers = (
                    connection.execute(
                        "SELECT ts,kind,value FROM change_markers ORDER BY id DESC LIMIT 101"
                    ).fetchall()
                    if connection
                    else []
                )
                content = encode(
                    Document(
                        "operations",
                        {
                            "history": history,
                            "weekly": weekly[:100],
                            "markers": [
                                (ts, kind, value if keep_app_names else "<omitted>")
                                for ts, kind, value in markers[:100]
                            ],
                            "truncated": history.truncated
                            or len(weekly) > 100
                            or len(markers) > 100,
                        },
                    ),
                    redactor=Redactor(tuple(private)),
                )
                bundle.add("operations.json", Redactor(tuple(private))(content))
        bundle.add(
            "manifest.json",
            encode(
                Document(
                    "report",
                    {
                        "package_version": __version__,
                        "fixture": fixture,
                        "app_names_kept": keep_app_names,
                        "omissions": bundle.omissions,
                        "privacy": "allowlisted machine inputs; no database or journal",
                    },
                )
            ),
        )
        selected = bundle if fixture else Bundle()
        if not fixture:
            for name in ("diagnostics.json", "manifest.json", "operations.json"):
                if name not in bundle.entries:
                    continue
                entry = bundle.entries[name]
                selected.add(entry.name, entry.data)
        # TarInfo is constructed from sanitized data only: no host metadata or hard links.
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            with tarfile.open(fileobj=stream, mode="w:gz") as archive:
                policy = Redactor(tuple(private))
                for entry in sorted(selected.entries.values(), key=lambda e: e.name):
                    member = tarfile.TarInfo(policy(entry.name))
                    member.uid = member.gid = member.mtime = 0
                    member.uname = member.gname = ""
                    member.mode = 0o700 if entry.directory else 0o600
                    if entry.directory:
                        member.type = tarfile.DIRTYPE
                        archive.addfile(member)
                    elif entry.link:
                        member.type = tarfile.SYMTYPE
                        member.linkname = policy(entry.link)
                        archive.addfile(member)
                    else:
                        data = policy(entry.data).encode()
                        member.size = len(data)
                        archive.addfile(member, io.BytesIO(data))
        return ReportView(str(output), fixture, len(selected.entries), tuple(bundle.omissions))
    except BaseException:
        if fd >= 0:
            os.close(fd)
        os.unlink(output)
        raise
