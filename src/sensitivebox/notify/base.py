"""The notifier interface and the hub that fans a notification out to every channel."""

import logging
from collections.abc import Iterable, Sequence
from typing import Protocol

from sensitivebox.alerts.engine import Transition
from sensitivebox.alerts.routing import Notification

log = logging.getLogger(__name__)


class Notifier(Protocol):
    """One delivery channel (desktop, webhook, sound…). Notifiers only ever see notifications
    that already passed the mute layer and cooldown."""

    async def notify(self, notification: Notification) -> bool | None:
        """False means delivery failed; None preserves simple successful notifiers."""
        ...


class NotifierHub:
    """Sends each notification to every notifier. One failing channel never stops the others,
    and never raises into the daemon loop. Return only notifications accepted by every enabled
    channel; retain successful channel receipts while a failed firing is retried."""

    def __init__(self, notifiers: Sequence[Notifier]) -> None:
        self._notifiers = tuple(notifiers)
        self._receipts: dict[tuple[str, str, float, bool], set[int]] = {}

    def discard_closed(self, alerts: Iterable[Transition]) -> None:
        """Keep partial channel receipts only while their firing is still open."""
        opened = {(t.rule_id, t.signal, t.opened_at, t.no_data) for t in alerts}
        for key in self._receipts.keys() - opened:
            del self._receipts[key]

    async def send(self, notifications: Iterable[Notification]) -> tuple[Notification, ...]:
        delivered: list[Notification] = []
        for notification in notifications:
            key = (
                notification.rule_id,
                notification.signal,
                notification.opened_at,
                notification.no_data,
            )
            receipts: set[int] = (
                self._receipts.setdefault(key, set()) if notification.kind == "firing" else set()
            )
            for index, notifier in enumerate(self._notifiers):
                if index in receipts:
                    continue
                try:
                    if await notifier.notify(notification) is not False:
                        receipts.add(index)
                except Exception:
                    log.exception("notifier %s failed", type(notifier).__name__)
            if len(receipts) == len(self._notifiers):
                delivered.append(notification)
                self._receipts.pop(key, None)
        return tuple(delivered)
