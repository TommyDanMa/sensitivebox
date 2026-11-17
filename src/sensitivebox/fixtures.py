"""Validated machine roots and read-only D-Bus snapshots; never fall back to host buses."""

import tarfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import msgspec
from dbus_fast import Message, MessageType, Variant

from sensitivebox.sources.bluez import BluetoothStatus, read_bluetooth
from sensitivebox.sources.wifi import WifiStatus
from sensitivebox.sources.wifi_nm import NM, NM_PATH, read_nm_wifi_once

type Properties = dict[str, dict[str, dict[str, str | bool | int]]]


class FixtureError(ValueError):
    """An invalid fixture, with instructions for repairing it."""


def _objects(path: Path) -> Properties:
    try:
        objects = msgspec.json.decode(path.read_bytes(), type=Properties)
        for interfaces in objects.values():
            for props in interfaces.values():
                for name, value in props.items():
                    expected = (
                        bool
                        if name in {"Powered", "Connected", "Paired", "Discoverable", "Discovering"}
                        else int
                        if name in {"Percentage", "Strength", "DeviceType", "Frequency"}
                        else str
                    )
                    if type(value) is not expected:
                        raise ValueError(f"invalid {name}")
        return objects
    except (OSError, ValueError, msgspec.DecodeError) as exc:
        raise FixtureError(f"cannot read {path}: {exc}; fix the fixture JSON and retry") from exc


@dataclass(frozen=True, slots=True)
class MachineRoot:
    path: Path
    bus: "SnapshotBus"
    temporary: TemporaryDirectory[str] | None = None

    @property
    def sys(self) -> Path:
        return self.path / "sys"

    @property
    def proc(self) -> Path:
        return self.path / "proc"

    @classmethod
    def load(cls, path: Path) -> "MachineRoot":
        if not all((path / name).is_dir() for name in ("sys", "proc", "dbus")):
            raise FixtureError(
                f"invalid root {path}; provide a machine folder with sys/, proc/, dbus/"
            )
        resolved = path.resolve()
        for entry in path.rglob("*"):
            if entry.is_symlink() and not entry.resolve().is_relative_to(resolved):
                raise FixtureError(f"fixture link {entry} escapes root; fix the link and retry")
        bluez = path / "dbus" / "bluez-managed-objects.json"
        nm = path / "dbus" / "nm-wifi.json"
        objects = _objects(bluez) if bluez.exists() else None
        network: dict[str, Properties] | None = None
        if nm.exists():
            try:
                network = msgspec.json.decode(nm.read_bytes(), type=dict[str, Properties])
                if set(network) != {"devices", "access_points"}:
                    raise ValueError("expected devices and access_points")
                # Validate the same property types as the BlueZ snapshot.
                for group in network.values():
                    for ifaces in group.values():
                        for props in ifaces.values():
                            for key, value in props.items():
                                expected = (
                                    int if key in {"Strength", "Frequency", "DeviceType"} else str
                                )
                                if type(value) is not expected:
                                    raise ValueError(f"invalid {key}")
            except (OSError, ValueError, msgspec.DecodeError) as exc:
                raise FixtureError(
                    f"cannot read {nm}: {exc}; fix the fixture JSON and retry"
                ) from exc
        return cls(path, SnapshotBus(objects, network))

    async def snapshot(self) -> tuple[BluetoothStatus, WifiStatus]:
        bt = await read_bluetooth(self.bus)
        wifi = await read_nm_wifi_once(self.bus)
        if self.bus.bluez is None:
            bt = BluetoothStatus(reason="BlueZ snapshot missing in fixture")
        if self.bus.nm is None:
            wifi = WifiStatus.unavailable("NetworkManager snapshot missing in fixture")
        return bt, wifi


class SnapshotBus:
    """Only the read methods used by the existing BlueZ/NM parsers; no socket connection."""

    def __init__(self, bluez: Properties | None, nm: dict[str, Properties] | None) -> None:
        self.bluez = bluez
        self.nm = nm

    async def call(self, msg: Message) -> Message:
        signature = ""
        body: list[Any] = []
        if msg.member in {"AddMatch", "RemoveMatch"}:
            pass
        elif msg.member == "GetManagedObjects" and self.bluez is not None:
            signature = "a{oa{sa{sv}}}"
            body = [
                {
                    p: {
                        i: {k: _variant(v) for k, v in props.items()} for i, props in ifaces.items()
                    }
                    for p, ifaces in self.bluez.items()
                }
            ]
        elif msg.destination == NM and self.nm is not None:
            if msg.path == NM_PATH and msg.member == "GetDevices":
                signature, body = "ao", [sorted(self.nm["devices"])]
            elif msg.member == "Get":
                interface, prop = str(msg.body[0]), str(msg.body[1])
                objects = {**self.nm["devices"], **self.nm["access_points"]}
                try:
                    value = objects[str(msg.path)][interface][prop]
                except KeyError:
                    return self._unavailable()
                signature, body = "v", [_variant(value)]
            else:
                return self._unavailable()
        else:
            return self._unavailable()
        return Message(
            message_type=MessageType.METHOD_RETURN, reply_serial=1, signature=signature, body=body
        )

    def _unavailable(self) -> Message:
        return Message(
            message_type=MessageType.ERROR,
            reply_serial=1,
            error_name="org.freedesktop.DBus.Error.Failed",
            signature="s",
            body=["service snapshot missing in fixture"],
        )

    def add_message_handler(self, handler: Callable[[Message], Any]) -> None:
        pass

    def remove_message_handler(self, handler: Callable[[Message], Any]) -> None:
        pass


def _variant(value: str | bool | int) -> Variant:
    signature = "b" if isinstance(value, bool) else "u" if isinstance(value, int) else "s"
    if isinstance(value, str) and value.startswith("/"):
        signature = "o"
    return Variant(signature, value)


def demo_root() -> MachineRoot:
    temporary = TemporaryDirectory(prefix="sensitivebox-demo-")
    try:
        archive = Path(__file__).parent / "data" / "proart-p16.tar.gz"
        with tarfile.open(archive) as bundle:
            bundle.extractall(temporary.name, filter="data")
        root = MachineRoot.load(Path(temporary.name) / "proart-p16")
        return replace(root, temporary=temporary)
    except BaseException:
        temporary.cleanup()
        raise
