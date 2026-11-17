"""Bluetooth status from BlueZ over the system D-Bus. Read-only in v0.1.

One ``ObjectManager.GetManagedObjects`` call returns every adapter and device. We report each
adapter's ``Powered`` state and the devices that are connected, with their ``Battery1`` level.
The toggle (a write) is v0.3 and lives elsewhere.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sensitivebox.sources.dbus import NO_BUS, Bus, DBusCallError, call, reason_for

BLUEZ = "org.bluez"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
BATTERY = "org.bluez.Battery1"
NO_ADAPTER = "no Bluetooth adapter"
MANAGED_OBJECTS = "a{oa{sa{sv}}}"  # GetManagedObjects' reply signature

type ManagedObjects = Mapping[str, Mapping[str, Mapping[str, Any]]]


@dataclass(frozen=True, slots=True)
class BtAdapter:
    name: str  # e.g. "hci0"
    alias: str
    powered: bool


@dataclass(frozen=True, slots=True)
class BtDevice:
    alias: str
    icon: str | None  # freedesktop icon name, e.g. "audio-headset"
    battery: int | None  # percent
    adapter: str  # adapter name, e.g. "hci0"


@dataclass(frozen=True, slots=True)
class BluetoothStatus:
    adapters: tuple[BtAdapter, ...] = ()
    devices: tuple[BtDevice, ...] = ()  # connected devices only
    reason: str | None = None  # set when unavailable

    @property
    def available(self) -> bool:
        return self.reason is None

    @property
    def powered(self) -> bool:
        return any(a.powered for a in self.adapters)


def _basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


def parse_managed_objects(objects: ManagedObjects) -> BluetoothStatus:
    adapters: list[BtAdapter] = []
    devices: list[BtDevice] = []
    for path in sorted(objects):
        interfaces = objects[path]
        if (adapter := interfaces.get(ADAPTER)) is not None:
            name = _basename(path)
            adapters.append(
                BtAdapter(name, str(adapter.get("Alias", name)), bool(adapter.get("Powered")))
            )
        device = interfaces.get(DEVICE)
        if device is None or not device.get("Connected"):
            continue
        battery = interfaces.get(BATTERY, {}).get("Percentage")
        icon = device.get("Icon")
        devices.append(
            BtDevice(
                alias=str(device.get("Alias", _basename(path))),
                icon=str(icon) if icon is not None else None,
                battery=int(battery) if battery is not None else None,
                adapter=_basename(str(device.get("Adapter", path.rsplit("/", 1)[0]))),
            )
        )
    if not adapters:
        return BluetoothStatus(reason=NO_ADAPTER)
    return BluetoothStatus(tuple(adapters), tuple(devices))


async def read_bluetooth(bus: Bus | None) -> BluetoothStatus:
    if bus is None:
        return BluetoothStatus(reason=NO_BUS)
    try:
        (objects,) = await call(
            bus, BLUEZ, "/", OBJECT_MANAGER, "GetManagedObjects", expected=MANAGED_OBJECTS
        )
    except DBusCallError as exc:
        return BluetoothStatus(reason=reason_for(exc, "BlueZ"))
    return parse_managed_objects(objects)
