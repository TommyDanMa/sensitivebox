"""Wi-Fi signal from NetworkManager over the system D-Bus, by subscription rather than polling.

``NmWifiWatcher.start`` reads the state once: the Wi-Fi device, its ``ActiveAccessPoint`` and
that access point's ``Strength``. After that it only listens for signals:

- ``PropertiesChanged`` on the access point (``Strength``),
- ``PropertiesChanged`` on the device (``ActiveAccessPoint``: a roam, a connect, a disconnect),
- ``NameOwnerChanged`` for NetworkManager (it stopped or restarted), and
- ``DeviceAdded``/``DeviceRemoved`` (a USB dongle plugged in or pulled out).

``current()`` returns the cached status, so the daemon's 1 s poll costs no D-Bus traffic. The
SSID is never read.
"""

import asyncio
import contextlib
from collections.abc import Coroutine
from typing import Any, cast

from dbus_fast import Message, MessageType

from sensitivebox.sources.dbus import (
    DBUS,
    INVALID_REPLY,
    NO_BUS,
    PROPERTIES,
    Bus,
    DBusCallError,
    add_match,
    call,
    get_property,
    reason_for,
    remove_match,
    remove_matches,
    signal_rule,
    unwrap,
)
from sensitivebox.sources.wifi import SOURCE_NM, WifiSignal, WifiStatus

NM = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"
DEVICE = f"{NM}.Device"
WIRELESS = f"{NM}.Device.Wireless"
ACCESS_POINT = f"{NM}.AccessPoint"
DEVICE_TYPE_WIFI = 2

NOT_RUNNING = f"{SOURCE_NM} not running"
NO_WIFI = "no Wi-Fi device"
NOT_CONNECTED = "Wi-Fi not connected"

NAME_OWNER_RULE = signal_rule(sender=DBUS, interface=DBUS, member="NameOwnerChanged", arg0=NM)
DEVICE_ADDED_RULE = signal_rule(sender=NM, interface=NM, member="DeviceAdded", path=NM_PATH)
DEVICE_REMOVED_RULE = signal_rule(sender=NM, interface=NM, member="DeviceRemoved", path=NM_PATH)


def props_rule(path: str, interface: str) -> str:
    return signal_rule(
        sender=NM, interface=PROPERTIES, member="PropertiesChanged", path=path, arg0=interface
    )


async def find_wifi_device(bus: Bus) -> tuple[str, str] | None:
    """``(object path, interface name)`` of the first Wi-Fi device, by path."""
    (paths,) = await call(bus, NM, NM_PATH, NM, "GetDevices", expected="ao")
    for path in sorted(str(p) for p in paths):
        if await get_property(bus, NM, path, DEVICE, "DeviceType") == DEVICE_TYPE_WIFI:
            return path, str(await get_property(bus, NM, path, DEVICE, "Interface"))
    return None


def signal_status(iface: str, strength: Any) -> WifiStatus:
    if not isinstance(strength, int):  # Strength is a byte; anything else is a broken reply
        raise DBusCallError(INVALID_REPLY, f"Strength is {type(strength).__name__}")
    percent = min(max(int(strength), 0), 100)
    return WifiStatus.ok(WifiSignal(iface, percent, None, SOURCE_NM))


async def read_nm_wifi_once(bus: Bus | None) -> WifiStatus:
    """One-shot read for ``sensitivebox doctor``; the daemon uses ``NmWifiWatcher``."""
    if bus is None:
        return WifiStatus.unavailable(NO_BUS)
    try:
        found = await find_wifi_device(bus)
        if found is None:
            return WifiStatus.unavailable(NO_WIFI)
        device, iface = found
        ap = str(await get_property(bus, NM, device, WIRELESS, "ActiveAccessPoint"))
        if ap in ("", "/"):
            return WifiStatus.unavailable(NOT_CONNECTED)
        return signal_status(iface, await get_property(bus, NM, ap, ACCESS_POINT, "Strength"))
    except DBusCallError as exc:
        return WifiStatus.unavailable(reason_for(exc, SOURCE_NM))


class NmWifiWatcher:
    def __init__(self) -> None:
        self._bus: Bus | None = None
        self._status = WifiStatus.unavailable("not started")
        self._device: str | None = None
        self._iface: str | None = None
        self._ap: str | None = None
        self._rules: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()

    def current(self) -> WifiStatus:
        return self._status

    async def start(self, bus: Bus | None) -> WifiStatus:
        """Subscribe and read once. Starting again on the same bus changes nothing; on another
        bus, the old subscriptions are dropped first (ADV-017)."""
        if bus is not None and bus is self._bus:
            return self._status
        await self.close()
        if bus is None:
            self._status = WifiStatus.unavailable(NO_BUS)
            return self._status
        self._bus = bus
        bus.add_message_handler(self._on_message)
        try:
            for rule in (NAME_OWNER_RULE, DEVICE_ADDED_RULE, DEVICE_REMOVED_RULE):
                await self._match(rule)
        except DBusCallError as exc:
            await self.close()
            self._status = WifiStatus.unavailable(reason_for(exc, "D-Bus"))
            return self._status
        await self._rescan()
        return self._status

    async def settle(self) -> None:
        """Wait for the follow-up reads that signals scheduled (tests, orderly shutdown)."""
        while self._tasks:
            await asyncio.gather(*self._tasks)

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        bus, self._bus = self._bus, None
        if bus is None:
            return
        bus.remove_message_handler(self._on_message)
        rules, self._rules = sorted(self._rules), set()
        self._device = self._iface = self._ap = None
        await remove_matches(bus, rules)

    # -- subscriptions ----------------------------------------------------------------------

    async def _match(self, rule: str) -> None:
        if self._bus is not None and rule not in self._rules:
            await add_match(self._bus, rule)
            self._rules.add(rule)

    async def _unmatch(self, rule: str) -> None:
        if self._bus is not None and rule in self._rules:
            self._rules.discard(rule)
            with contextlib.suppress(DBusCallError):
                await remove_match(self._bus, rule)

    async def _forget_device(self) -> None:
        await self._forget_ap()
        if self._device is not None:
            await self._unmatch(props_rule(self._device, WIRELESS))
        self._device = self._iface = None

    async def _forget_ap(self) -> None:
        if self._ap is not None:
            await self._unmatch(props_rule(self._ap, ACCESS_POINT))
        self._ap = None

    # -- state ------------------------------------------------------------------------------

    async def _rescan(self) -> None:
        async with self._lock:
            bus = self._bus
            if bus is None:
                return
            try:
                found = await find_wifi_device(bus)
                if found is None:
                    await self._forget_device()
                    self._status = WifiStatus.unavailable(NO_WIFI)
                    return
                device, iface = found
                if device != self._device:
                    await self._forget_device()
                    # Subscribe before reading, so a change in between isn't missed.
                    await self._match(props_rule(device, WIRELESS))
                    self._device, self._iface = device, iface
                ap = str(await get_property(bus, NM, device, WIRELESS, "ActiveAccessPoint"))
                await self._follow_ap_locked(ap)
            except DBusCallError as exc:
                await self._forget_device()
                self._status = WifiStatus.unavailable(reason_for(exc, SOURCE_NM))

    async def _follow_ap(self, ap: str) -> None:
        async with self._lock:
            try:
                await self._follow_ap_locked(ap)
            except DBusCallError as exc:
                await self._forget_ap()
                self._status = WifiStatus.unavailable(reason_for(exc, SOURCE_NM))

    async def _follow_ap_locked(self, ap: str) -> None:
        if self._bus is None or self._iface is None:
            return
        if ap in ("", "/"):
            await self._forget_ap()
            self._status = WifiStatus.unavailable(NOT_CONNECTED)
            return
        if ap != self._ap:
            await self._forget_ap()
            await self._match(props_rule(ap, ACCESS_POINT))
            self._ap = ap
        strength = await get_property(self._bus, NM, ap, ACCESS_POINT, "Strength")
        self._status = signal_status(self._iface, strength)

    async def _nm_gone(self) -> None:
        async with self._lock:
            await self._forget_device()
            self._status = WifiStatus.unavailable(NOT_RUNNING)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- signals ----------------------------------------------------------------------------

    def _on_message(self, msg: Message) -> None:
        if msg.message_type != MessageType.SIGNAL:
            return
        body: list[Any] = msg.body
        if msg.interface == DBUS and msg.member == "NameOwnerChanged":
            if len(body) == 3 and body[0] == NM:
                self._spawn(self._rescan() if body[2] else self._nm_gone())
        elif msg.interface == PROPERTIES and msg.member == "PropertiesChanged":
            self._on_properties_changed(msg.path, body)
        elif (
            msg.interface == NM
            and msg.path == NM_PATH
            and msg.member
            in (
                "DeviceAdded",
                "DeviceRemoved",
            )
        ):
            self._spawn(self._rescan())

    def _on_properties_changed(self, path: str | None, body: list[Any]) -> None:
        if len(body) < 2 or not isinstance(body[1], dict):
            return
        interface: Any = body[0]
        changed: dict[str, Any] = unwrap(body[1])
        # A property can also be *invalidated*: it changed, but the new value isn't in the
        # signal and has to be read (D-Bus spec, PropertiesChanged; review ADV-011).
        names = cast(list[Any], body[2]) if len(body) > 2 and isinstance(body[2], list) else []
        invalidated = {str(name) for name in names}
        ap = self._ap
        if ap is not None and path == ap and interface == ACCESS_POINT:
            if "Strength" in changed and self._iface is not None:
                self._status = signal_status(self._iface, changed["Strength"])
            elif "Strength" in invalidated:
                self._spawn(self._follow_ap(ap))  # re-reads Strength
        elif path == self._device and interface == WIRELESS:
            if "ActiveAccessPoint" in changed:
                self._spawn(self._follow_ap(str(changed["ActiveAccessPoint"])))
            elif "ActiveAccessPoint" in invalidated:
                self._spawn(self._rescan())  # re-reads ActiveAccessPoint
