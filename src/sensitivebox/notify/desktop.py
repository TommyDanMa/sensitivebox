"""Desktop notifications over ``org.freedesktop.Notifications`` on the session bus.

Never ``notify-send`` and never a subprocess: one D-Bus method call per notification. Urgency
follows the spec's hint (0 low, 1 normal, 2 critical): a critical alert that fires is 2, a
resolved alert is low, and anything else that fires is normal.
"""

import logging
from collections.abc import Callable
from typing import Final

from dbus_fast import Variant
from dbus_fast.errors import DBusFastError

from sensitivebox.alerts.engine import TransitionKind
from sensitivebox.alerts.routing import Notification
from sensitivebox.sources.dbus import Bus, DBusCallError, call

log = logging.getLogger(__name__)

SERVICE: Final = "org.freedesktop.Notifications"
PATH: Final = "/org/freedesktop/Notifications"
APP_NAME: Final = "Sensitive Box"

URGENCY_LOW: Final = 0
URGENCY_NORMAL: Final = 1
URGENCY_CRITICAL: Final = 2
EXPIRE_DEFAULT: Final = -1  # let the notification server decide


def urgency(notification: Notification) -> int:
    if notification.kind is TransitionKind.RESOLVED:
        return URGENCY_LOW
    return URGENCY_CRITICAL if notification.severity == "critical" else URGENCY_NORMAL


class DesktopNotifier:
    """Sends notifications on ``bus``. With no bus (no desktop session) it does nothing.

    A failed call is logged once per distinct error and returns False so the daemon can retry.
    A missing notification daemon must not stop alerting, the dashboard or the log.
    """

    def __init__(self, bus: Bus | None, *, warn: Callable[[str], None] = log.warning) -> None:
        self._bus = bus
        self._warn = warn
        self._warned: set[str] = set()
        if bus is None:
            self._warn_once("no-session", "desktop notifications off: no session bus")

    async def notify(self, notification: Notification) -> bool | None:
        if self._bus is None:
            return
        try:
            await call(
                self._bus,
                SERVICE,
                PATH,
                SERVICE,
                "Notify",
                "susssasa{sv}i",
                [
                    APP_NAME,
                    0,  # replaces_id: a new notification each time
                    "",  # app_icon
                    notification.title,
                    notification.body,
                    [],  # actions
                    {"urgency": Variant("y", urgency(notification))},
                    EXPIRE_DEFAULT,
                ],
            )
        except DBusCallError as exc:
            self._warn_once(exc.name, f"desktop notification failed: {exc}")
            return False
        except (OSError, EOFError, DBusFastError) as exc:  # the bus went away
            self._warn_once(type(exc).__name__, f"desktop notification failed: {exc!r}")
            return False
        return True

    def _warn_once(self, key: str, message: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self._warn(message)
