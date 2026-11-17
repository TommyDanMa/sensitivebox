"""Sleep and shutdown notices from systemd-logind.

``PrepareForSleep(true)`` is sent just before suspend or hibernate and ``PrepareForSleep(false)``
just after resume; ``PrepareForShutdown(true)`` precedes shutdown or reboot. While either is in
progress the daemon pauses evaluation and the missing-data timers, so closing the lid or shutting
down never fires an alert, and never a "no data" one. On resume the engine restarts every rule in
Warming (docs/BIBLE.md, "Sleep and shutdown").

Sleep and shutdown are tracked separately, so the pause lasts until neither is in progress.
"""

from collections.abc import Callable

from dbus_fast import Message, MessageType

from sensitivebox.sources.dbus import Bus, DBusCallError, add_match, remove_matches, signal_rule

LOGIND = "org.freedesktop.login1"
LOGIND_PATH = "/org/freedesktop/login1"
MANAGER = "org.freedesktop.login1.Manager"
SIGNALS = ("PrepareForSleep", "PrepareForShutdown")
RULES = tuple(
    signal_rule(sender=LOGIND, interface=MANAGER, member=member, path=LOGIND_PATH)
    for member in SIGNALS
)


class LogindWatcher:
    """Calls ``on_suspend`` when the first of sleep/shutdown begins and ``on_resume`` when the
    last one ends. Both run inside the D-Bus message loop, so they must be quick."""

    def __init__(self, on_suspend: Callable[[], None], on_resume: Callable[[], None]) -> None:
        self._on_suspend = on_suspend
        self._on_resume = on_resume
        self._bus: Bus | None = None
        self._rules: list[str] = []
        self._preparing: set[str] = set()

    @property
    def preparing(self) -> bool:
        """True between a ``Prepare…(true)`` and the matching ``(false)``."""
        return bool(self._preparing)

    async def start(self, bus: Bus | None) -> bool:
        """Subscribe. ``False`` when there is no bus or logind refuses the subscription. Starting
        again on the same bus changes nothing; on another bus, the old subscriptions are dropped
        first (ADV-017)."""
        if bus is not None and bus is self._bus:
            return True
        await self.close()
        if bus is None:
            return False
        self._bus = bus
        bus.add_message_handler(self._on_message)
        try:
            for rule in RULES:
                await add_match(bus, rule)
                self._rules.append(rule)
        except DBusCallError:
            await self.close()
            return False
        return True

    async def close(self) -> None:
        bus, self._bus = self._bus, None
        if bus is None:
            return
        bus.remove_message_handler(self._on_message)
        rules, self._rules = self._rules, []
        await remove_matches(bus, rules)

    def _on_message(self, msg: Message) -> None:
        if (
            msg.message_type != MessageType.SIGNAL
            or msg.interface != MANAGER
            or msg.member not in SIGNALS
            or len(msg.body) != 1
        ):
            return
        was_preparing = self.preparing
        if msg.body[0]:
            self._preparing.add(str(msg.member))
        else:
            self._preparing.discard(str(msg.member))
        if self.preparing and not was_preparing:
            self._on_suspend()
        elif was_preparing and not self.preparing:
            self._on_resume()
