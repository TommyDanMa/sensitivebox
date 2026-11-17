"""D-Bus: the one place that opens a connection, plus small call helpers.

Everything else takes a ``Bus`` (a narrow protocol that dbus-fast's ``MessageBus`` satisfies), so
tests can drive a fake one. Sources only make calls that read state (``Get``,
``GetManagedObjects``, ``GetDevices``, ``AddMatch``/``RemoveMatch`` for signal subscriptions). The
one call that does something is ``Notify`` on the *session* bus, from ``notify.desktop``.
"""

import asyncio
import contextlib
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusFastError

DBUS = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
PROPERTIES = "org.freedesktop.DBus.Properties"
NO_BUS = "no system bus"
CONNECT_TIMEOUT_S = 2.0
# A local bus answers in milliseconds. dbus-fast waits for a reply forever, so every call gets a
# deadline: a stalled service must never block a watcher's start or the daemon's shutdown.
CALL_TIMEOUT_S = 1.0
NO_REPLY = "org.freedesktop.DBus.Error.NoReply"
INVALID_REPLY = "org.freedesktop.DBus.Error.InvalidSignature"

_NOT_RUNNING = {
    "org.freedesktop.DBus.Error.ServiceUnknown",
    "org.freedesktop.DBus.Error.NameHasNoOwner",
    "org.freedesktop.DBus.Error.Spawn.ChildExited",
    "org.freedesktop.systemd1.NoSuchUnit",
}

type Handler = Callable[[Message], Message | bool | None]


class Bus(Protocol):
    async def call(self, msg: Message) -> Message | None: ...
    def add_message_handler(self, handler: Handler) -> None: ...
    def remove_message_handler(self, handler: Handler) -> None: ...


class DBusCallError(Exception):
    def __init__(self, name: str, text: str = "") -> None:
        super().__init__(f"{name}: {text}" if text else name)
        self.name = name

    @property
    def not_running(self) -> bool:
        return self.name in _NOT_RUNNING


def reason_for(exc: DBusCallError, service: str) -> str:
    """A short, user-facing reason, e.g. ``NetworkManager not running``."""
    return f"{service} not running" if exc.not_running else f"{service} error ({exc.name})"


def unwrap(value: Any) -> Any:
    """Strip dbus-fast ``Variant`` wrappers, recursively."""
    if isinstance(value, Variant):
        return unwrap(value.value)
    if isinstance(value, dict):
        return {k: unwrap(v) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, list):
        return [unwrap(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
    return value


async def call(
    bus: Bus,
    destination: str,
    path: str,
    interface: str,
    member: str,
    signature: str = "",
    body: Sequence[Any] = (),
    *,
    expected: str | None = None,
) -> list[Any]:
    """Call a method and return its unwrapped body. Raises ``DBusCallError`` on an error reply,
    with ``NoReply`` when no answer comes within ``CALL_TIMEOUT_S``, and with
    ``InvalidSignature`` when ``expected`` names the reply signature and the service sent
    something else (ADV-018): a malformed reply makes a source unavailable, never a crash."""
    message = Message(
        destination=destination,
        path=path,
        interface=interface,
        member=member,
        signature=signature,
        body=list(body),
    )
    try:
        async with asyncio.timeout(CALL_TIMEOUT_S):
            reply = await bus.call(message)
    except TimeoutError:
        raise DBusCallError(
            NO_REPLY, f"no answer to {member} within {CALL_TIMEOUT_S:g} s"
        ) from None
    if reply is None:
        raise DBusCallError("no-reply")
    if reply.message_type == MessageType.ERROR:
        text = reply.body[0] if reply.body and isinstance(reply.body[0], str) else ""
        raise DBusCallError(reply.error_name or "unknown", text)
    if expected is not None and reply.signature != expected:
        raise DBusCallError(
            INVALID_REPLY, f"{member} replied {reply.signature!r}, not {expected!r}"
        )
    return [unwrap(v) for v in reply.body]


async def get_property(bus: Bus, destination: str, path: str, interface: str, name: str) -> Any:
    (value,) = await call(
        bus, destination, path, PROPERTIES, "Get", "ss", [interface, name], expected="v"
    )
    return value


async def add_match(bus: Bus, rule: str) -> None:
    await call(bus, DBUS, DBUS_PATH, DBUS, "AddMatch", "s", [rule], expected="")


async def remove_match(bus: Bus, rule: str) -> None:
    await call(bus, DBUS, DBUS_PATH, DBUS, "RemoveMatch", "s", [rule], expected="")


async def remove_matches(bus: Bus, rules: Sequence[str]) -> None:
    """Drop match rules on the way out: all at once, under one deadline, never raising. The bus
    or the service may already be gone or stalled, and shutdown must not wait on either."""
    if not rules:
        return
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(CALL_TIMEOUT_S):
            await asyncio.gather(
                *(remove_match(bus, rule) for rule in rules), return_exceptions=True
            )


def signal_rule(
    *,
    sender: str,
    interface: str,
    member: str,
    path: str | None = None,
    path_namespace: str | None = None,
    arg0: str | None = None,
) -> str:
    parts = [f"type='signal',sender='{sender}',interface='{interface}',member='{member}'"]
    if path is not None:
        parts.append(f"path='{path}'")
    if path_namespace is not None:
        parts.append(f"path_namespace='{path_namespace}'")
    if arg0 is not None:
        parts.append(f"arg0='{arg0}'")
    return ",".join(parts)


async def _connect(bus_type: BusType) -> MessageBus | None:
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT_S):
            return await MessageBus(bus_type=bus_type).connect()
    except (OSError, DBusFastError):  # TimeoutError is an OSError
        return None


async def connect_system_bus() -> MessageBus | None:
    """``None`` when there is no system bus to talk to (containers, CI, tests)."""
    return await _connect(BusType.SYSTEM)


async def connect_session_bus() -> MessageBus | None:
    """The user's session bus, where desktop notifications live. ``None`` without a session."""
    return await _connect(BusType.SESSION)
