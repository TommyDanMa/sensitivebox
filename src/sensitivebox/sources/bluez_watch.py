"""Bluetooth status kept current by BlueZ's signals, never polled.

``read_bluetooth`` is one ``GetManagedObjects`` call: right for ``doctor``, wrong for a 1 s loop.
The daemon uses this watcher instead, as it does ``NmWifiWatcher`` for Wi-Fi:

* it subscribes first, then reads the object tree **once**;
* ``InterfacesAdded``/``InterfacesRemoved`` and ``PropertiesChanged`` on ``Adapter1``,
  ``Device1`` and ``Battery1`` update that tree in memory, and the status is re-derived from it
  with the same parser ``doctor`` uses;
* ``NameOwnerChanged`` for ``org.bluez`` handles ``bluetoothd`` stopping (status unavailable)
  and starting (one more read).

``current()`` is a plain attribute read: no D-Bus call per poll, ever.
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from dbus_fast import Message, MessageType

from sensitivebox.sources.bluez import (
    ADAPTER,
    BATTERY,
    BLUEZ,
    DEVICE,
    MANAGED_OBJECTS,
    OBJECT_MANAGER,
    BluetoothStatus,
    parse_managed_objects,
)
from sensitivebox.sources.dbus import (
    DBUS,
    NO_BUS,
    PROPERTIES,
    Bus,
    DBusCallError,
    add_match,
    call,
    reason_for,
    remove_matches,
    signal_rule,
    unwrap,
)

log = logging.getLogger(__name__)

BLUEZ_PATH = "/org/bluez"
NOT_RUNNING = "BlueZ not running"
WATCHED = (ADAPTER, DEVICE, BATTERY)

NAME_OWNER_RULE = signal_rule(sender=DBUS, interface=DBUS, member="NameOwnerChanged", arg0=BLUEZ)
ADDED_RULE = signal_rule(sender=BLUEZ, interface=OBJECT_MANAGER, member="InterfacesAdded", path="/")
REMOVED_RULE = signal_rule(
    sender=BLUEZ, interface=OBJECT_MANAGER, member="InterfacesRemoved", path="/"
)
CHANGED_RULE = signal_rule(
    sender=BLUEZ, interface=PROPERTIES, member="PropertiesChanged", path_namespace=BLUEZ_PATH
)
RULES = (NAME_OWNER_RULE, ADDED_RULE, REMOVED_RULE, CHANGED_RULE)

type Objects = dict[str, dict[str, dict[str, Any]]]


class BluezWatcher:
    """``on_change`` is called with the new status whenever it actually changes. It runs inside
    the D-Bus message loop, so it must be quick and must not raise."""

    def __init__(self, on_change: Callable[[BluetoothStatus], None] | None = None) -> None:
        self._on_change = on_change
        self._bus: Bus | None = None
        self._status = BluetoothStatus(reason="not started")
        self._objects: Objects = {}
        self._rules: list[str] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        self._loading = False
        self._pending: list[Message] = []

    def current(self) -> BluetoothStatus:
        return self._status

    async def start(self, bus: Bus | None) -> BluetoothStatus:
        """Subscribe and read once. Starting again on the same bus changes nothing; on another
        bus, the old subscriptions are dropped first (ADV-017)."""
        if bus is not None and bus is self._bus:
            return self._status
        await self.close()
        if bus is None:
            self._set(BluetoothStatus(reason=NO_BUS))
            return self._status
        self._bus = bus
        bus.add_message_handler(self._on_message)
        try:
            for rule in RULES:
                await add_match(bus, rule)
                self._rules.append(rule)
        except DBusCallError as exc:
            await self.close()
            self._set(BluetoothStatus(reason=reason_for(exc, "D-Bus")))
            return self._status
        await self._load()  # subscribed first, so a change during the read isn't missed
        return self._status

    async def settle(self) -> None:
        """Wait for the reads that signals scheduled (tests, orderly shutdown)."""
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
        rules, self._rules = self._rules, []
        await remove_matches(bus, rules)

    # -- state --------------------------------------------------------------------------------

    def _set(self, status: BluetoothStatus) -> None:
        if status == self._status:
            return
        self._status = status
        if self._on_change is not None:
            try:
                self._on_change(status)
            except Exception:
                log.exception("bluetooth change callback failed")

    def _refresh(self) -> None:
        self._set(parse_managed_objects(self._objects))

    async def _load(self) -> None:
        """The one read of the whole object tree, then replay what arrived while it ran."""
        async with self._lock:
            bus = self._bus
            if bus is None:
                return
            self._loading, self._pending = True, []
            try:
                (objects,) = await call(
                    bus, BLUEZ, "/", OBJECT_MANAGER, "GetManagedObjects", expected=MANAGED_OBJECTS
                )
            except DBusCallError as exc:
                self._objects = {}
                self._set(BluetoothStatus(reason=reason_for(exc, "BlueZ")))
            else:
                self._objects = {
                    path: {iface: dict(props) for iface, props in ifaces.items()}
                    for path, ifaces in objects.items()
                }
                self._refresh()
            finally:
                self._loading = False
                pending, self._pending = self._pending, []
            # Replaying a signal the snapshot already reflects is harmless: the last signal
            # about any property is also the newest, so the end state is the right one.
            for message in pending:
                self._apply(message)

    def _gone(self) -> None:
        self._objects = {}
        self._set(BluetoothStatus(reason=NOT_RUNNING))

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- signals ------------------------------------------------------------------------------

    def _on_message(self, msg: Message) -> None:
        if msg.message_type != MessageType.SIGNAL:
            return
        if self._loading:
            self._pending.append(msg)
        else:
            self._apply(msg)

    def _apply(self, msg: Message) -> None:
        body: list[Any] = [unwrap(part) for part in msg.body]
        path = msg.path or ""
        if msg.interface == DBUS and msg.member == "NameOwnerChanged":
            if len(body) == 3 and body[0] == BLUEZ:
                if body[2]:
                    self._spawn(self._load())  # bluetoothd (re)started: read the tree once
                else:
                    self._gone()
        elif msg.interface == OBJECT_MANAGER and msg.member == "InterfacesAdded":
            if len(body) == 2:
                added = self._objects.setdefault(str(body[0]), {})
                for iface, props in body[1].items():
                    added[iface] = dict(props)
                self._refresh()
        elif msg.interface == OBJECT_MANAGER and msg.member == "InterfacesRemoved":
            if len(body) == 2:
                existing = self._objects.get(str(body[0]), {})
                for iface in body[1]:
                    existing.pop(iface, None)
                if not existing:
                    self._objects.pop(str(body[0]), None)
                self._refresh()
        elif msg.interface == PROPERTIES and msg.member == "PropertiesChanged":
            watched = path.startswith(BLUEZ_PATH) and len(body) == 3 and body[0] in WATCHED
            props = self._objects.get(path, {}).get(body[0]) if watched else None
            if props is not None:
                props.update(body[1])
                for name in body[2]:
                    props.pop(name, None)
                self._refresh()
