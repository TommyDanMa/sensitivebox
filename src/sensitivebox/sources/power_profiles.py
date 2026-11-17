"""Read and subscribe to ActiveProfile, supporting current and legacy services."""

import asyncio
from collections.abc import Callable
from typing import Any, cast

from dbus_fast import Message, MessageType

from sensitivebox.sources.dbus import (
    DBUS,
    PROPERTIES,
    Bus,
    DBusCallError,
    add_match,
    get_property,
    remove_matches,
    signal_rule,
    unwrap,
)

SERVICES = (
    ("org.freedesktop.UPower.PowerProfiles", "/org/freedesktop/UPower/PowerProfiles"),
    ("net.hadess.PowerProfiles", "/net/hadess/PowerProfiles"),
)


class PowerProfileWatcher:
    def __init__(self, on_change: Callable[[str], None]) -> None:
        self.on_change = on_change
        self.bus: Bus | None = None
        self.rules: list[str] = []
        self.tasks: set[asyncio.Task[None]] = set()

    async def start(self, bus: Bus | None) -> bool:
        await self.close()
        if bus is None:
            return False
        self.bus = bus
        bus.add_message_handler(self._message)
        try:
            for service, path in SERVICES:
                for rule in (
                    signal_rule(
                        sender=service,
                        interface=PROPERTIES,
                        member="PropertiesChanged",
                        path=path,
                        arg0=service,
                    ),
                    signal_rule(
                        sender=DBUS, interface=DBUS, member="NameOwnerChanged", arg0=service
                    ),
                ):
                    await add_match(bus, rule)
                    self.rules.append(rule)
            await self._read()
        except DBusCallError:
            await self.close()
            return False
        return True

    async def _read(self) -> None:
        if self.bus is None:
            return
        for service, path in SERVICES:
            try:
                value: Any = await get_property(self.bus, service, path, service, "ActiveProfile")
            except DBusCallError:
                continue
            if isinstance(value, str):
                self.on_change(value)
                return

    def _message(self, msg: Message) -> None:
        if msg.message_type != MessageType.SIGNAL:
            return
        if (
            msg.interface == PROPERTIES
            and msg.member == "PropertiesChanged"
            and any(msg.path == p for _, p in SERVICES)
            and len(msg.body) == 3
            and msg.body[0] in {s for s, _ in SERVICES}
        ):
            props: Any = unwrap(msg.body[1])
            value: Any = (
                cast(dict[str, Any], props).get("ActiveProfile")
                if isinstance(props, dict)
                else None
            )
            if isinstance(value, str):
                self.on_change(value)
            elif "ActiveProfile" in msg.body[2]:
                self._refresh()
        elif (
            msg.interface == DBUS
            and msg.member == "NameOwnerChanged"
            and len(msg.body) == 3
            and msg.body[0] in {s for s, _ in SERVICES}
            and msg.body[2]
        ):
            self._refresh()

    def _refresh(self) -> None:
        task = asyncio.create_task(self._read())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        bus, self.bus = self.bus, None
        if bus is not None:
            bus.remove_message_handler(self._message)
            rules, self.rules = self.rules, []
            await remove_matches(bus, rules)
