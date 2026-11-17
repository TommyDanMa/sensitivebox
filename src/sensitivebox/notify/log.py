"""A notifier that prints one line per alert to the terminal: what ``sbox watch`` shows."""

import time
from collections.abc import Callable

from sensitivebox.alerts.engine import TransitionKind
from sensitivebox.alerts.routing import Notification
from sensitivebox.redact import redact


class LogNotifier:
    def __init__(
        self,
        write: Callable[[str], None] = print,
        *,
        now: Callable[[], float] = time.time,
        event: Callable[[Notification, str], None] | None = None,
    ) -> None:
        self._write = write
        self._now = now
        self._event = event

    async def notify(self, notification: Notification) -> None:
        stamp = time.strftime("%H:%M:%S", time.localtime(self._now()))
        word = "FIRING  " if notification.kind is TransitionKind.FIRING else "RESOLVED"
        text = f"{stamp}  {word}  {notification.title} — {notification.body}"
        if self._event is not None:
            self._event(notification, text)
        else:
            self._write(redact(text))
