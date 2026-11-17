"""Latest poll and bounded live subscriptions, owned by the daemon's asyncio loop."""

import asyncio
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field, replace

from sensitivebox.alerts.engine import Clock, Engine
from sensitivebox.alerts.model import Missing
from sensitivebox.api.data import incident
from sensitivebox.api.model import (
    AppView,
    BluetoothView,
    EvaluationView,
    IncidentView,
    SensorsView,
    SignalView,
)
from sensitivebox.collector import Collector
from sensitivebox.daemon import TickResult
from sensitivebox.hwmon.model import Group, Limits
from sensitivebox.redact import device_label, redacted_json
from sensitivebox.sources.bluez import BluetoothStatus
from sensitivebox.sources.wifi import WifiStatus
from sensitivebox.store import Store


@dataclass(frozen=True, slots=True)
class AlertUpdate:
    incident: IncidentView
    kind: str
    reason: str | None = None


@dataclass(eq=False, slots=True)
class Subscription:
    queue: asyncio.Queue[str | None]
    closed: asyncio.Event = field(default_factory=asyncio.Event)

    def close(self) -> None:
        self.closed.set()
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put_nowait(None)


class ApiState:
    queue_size = 64

    def __init__(
        self,
        store: Store,
        collector: Collector,
        *,
        engine: Engine,
        clock: Clock,
        wall: Clock,
        boot: str,
        interval: float,
        bluetooth: Callable[[], BluetoothStatus] = lambda: BluetoothStatus(reason="not available"),
        wifi: Callable[[], WifiStatus] = lambda: WifiStatus.unavailable("not available"),
    ) -> None:
        self.store, self.collector = store, collector
        self.engine = engine
        self.clock, self.wall, self.boot, self.interval = clock, wall, boot, interval
        self._bluetooth, self._wifi = bluetooth, wifi
        self._snapshot: SensorsView | None = None
        self._at: float | None = None
        self._serial = 0
        self._subscriptions: set[Subscription] = set()
        self._private: set[str] = set()
        self._stable = {s.id for s in collector.sensors}
        self._event_cursor = int(
            store.mutes.db.execute("SELECT coalesce(max(id),0) FROM alert_events").fetchone()[0]
        )

    @property
    def subscriber_count(self) -> int:
        return len(self._subscriptions)

    def encode(self, value: object) -> str:
        return redacted_json(
            value,
            private=tuple(sorted(self._private, key=len, reverse=True)),
            stable_ids=frozenset(self._stable),
        )

    def snapshot(self) -> SensorsView | None:
        if self._snapshot is None or self._at is None:
            return None
        return replace(self._snapshot, fresh=0 <= self.clock() - self._at <= 3 * self.interval)

    def tick(self, result: TickResult) -> None:
        snap = result.snapshot
        self._at = snap.at
        sensors = {sensor.id: sensor for sensor in self.collector.sensors}
        self._stable.update(snap.signals)
        self._stable.update(snap.metrics)
        self._stable.update(snap.display)
        self._private.update(app.name for app in snap.apps)
        bluetooth = self._bluetooth()
        self._private.update(d.alias for d in bluetooth.devices)
        self._private.update(a.alias for a in bluetooth.adapters)
        groups: dict[str, list[SignalView]] = {group.value: [] for group in Group}
        values = dict(snap.display)
        values.update(snap.metrics)
        values.update(snap.signals)
        rules = {rule.id: rule for rule in self.engine.rules}
        evaluations: dict[str, list[EvaluationView]] = {}
        for evaluation in result.step.evaluations:
            rule = rules[evaluation.rule_id]
            threshold = evaluation.threshold
            if rule.when == "equals":
                threshold = float(rule.value) if isinstance(rule.value, (int, float)) else None
            evaluations.setdefault(evaluation.signal, []).append(
                EvaluationView(
                    rule.id,
                    evaluation.state,
                    rule.when,
                    threshold,
                    self.engine.resolved_clear(rule.id, evaluation.signal),
                    rule.severity,
                )
            )
        for identity, value in sorted(values.items()):
            sensor = sensors.get(identity)
            group = sensor.group.value if sensor is not None else _group(identity)
            groups[group].append(
                SignalView(
                    identity,
                    sensor.label if sensor is not None else _label(identity),
                    sensor.unit if sensor is not None else snap.units.get(identity, ""),
                    sensor.limits
                    if sensor is not None
                    else self.collector.limits.get(identity, Limits()),
                    None if isinstance(value, Missing) else value,
                    value.reason if isinstance(value, Missing) else None,
                    tuple(evaluations.get(identity, ())),
                )
            )
        self._snapshot = SensorsView(
            self.wall(),
            True,
            {g: tuple(v) for g, v in groups.items()},
            tuple(AppView("<redacted>", app.bytes) for app in snap.apps),
            BluetoothView(
                bluetooth.powered if bluetooth.available else None,
                tuple(device_label(d) for d in bluetooth.devices),
                bluetooth.reason,
            ),
            self._wifi(),
        )
        self._publish("snapshot", self._snapshot)
        for event in reversed(self.store.events.history(after=self._event_cursor, limit=10000)):
            self._event_cursor = event.id
            current = incident(self.store.mutes.db, event.incident_id)
            if current is not None:
                self._publish("alert", AlertUpdate(current, event.kind, event.reason))

    def changed(self, current: IncidentView, kind: str) -> None:
        self._publish("alert", AlertUpdate(current, kind))

    def _event(self, name: str, payload: object) -> str:
        self._serial += 1
        return f"id: {self._serial}\nevent: {name}\ndata: {self.encode(payload)}\n\n"

    def _publish(self, name: str, payload: object) -> None:
        event = self._event(name, payload)
        for sub in tuple(self._subscriptions):
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                sub.close()
                self._subscriptions.discard(sub)

    def subscribe(self) -> Subscription:
        sub = Subscription(asyncio.Queue(self.queue_size))
        self._subscriptions.add(sub)
        current = self.snapshot()
        if current is not None:
            sub.queue.put_nowait(self._event("snapshot", current))
        return sub

    async def stream(self, sub: Subscription | None = None) -> AsyncGenerator[str]:
        sub = sub or self.subscribe()
        try:
            while not sub.closed.is_set():
                event = await sub.queue.get()
                if event is None or sub.closed.is_set():
                    break
                yield event
        finally:
            self.unsubscribe(sub)

    def unsubscribe(self, sub: Subscription) -> None:
        self._subscriptions.discard(sub)
        sub.close()

    def close(self) -> None:
        for sub in tuple(self._subscriptions):
            self.unsubscribe(sub)


def _group(identity: str) -> str:
    if identity.startswith(("cpu/", "psi/cpu/")):
        return "CPU"
    if identity.startswith(("memory/", "zram/", "vmstat/", "psi/memory/")):
        return "Memory"
    if identity.startswith("gpu/"):
        return "GPU"
    if identity.startswith("psi/io/"):
        return "Storage"
    if identity.startswith(("net/", "wifi/")):
        return "Network"
    if identity.startswith("power_supply/"):
        return "Power"
    return "System"


def _label(identity: str) -> str:
    return identity.replace("_", " ").replace("/", " · ")
