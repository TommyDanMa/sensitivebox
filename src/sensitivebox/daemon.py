"""The asyncio main loop: poll, evaluate, store, route, notify, once per interval.

One ``tick`` is: collect a snapshot, feed the alert engine (which journals its inputs into the
store, so they can be replayed), hand the metrics and the rule evaluations to the store, route
the engine's transitions through mute and cooldown, and send what is left to the notifiers. The
engine and the collector share one monotonic clock, injected, so tests drive the whole loop with
a manual clock and no sleeping.

Sleep: ``on_suspend``/``on_resume`` (wired to logind) pause everything. A suspended daemon skips
its ticks, so closing the lid never fires an alert and never a "no data" one. On resume every
rule warms up again and the collector starts its rates and deltas from a fresh baseline.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from sensitivebox.alerts.engine import Clock, Engine, Step
from sensitivebox.alerts.model import State
from sensitivebox.alerts.routing import Held, Notification, Routed, Router
from sensitivebox.collector import Snapshot
from sensitivebox.notify.base import NotifierHub
from sensitivebox.store import Store
from sensitivebox.store.mutes import ActiveMute

log = logging.getLogger(__name__)

NOTIFY_TIMEOUT_S = 2.0  # a hung notification daemon may delay a poll this long, no longer
LOG_EVERY = 60  # a poll that keeps failing is logged on the 1st failure and every 60th


class SignalSource(Protocol):
    """What the daemon polls: the real :class:`Collector`, or the simulator."""

    def collect(self) -> Snapshot: ...
    def reset_baselines(self) -> None: ...


@dataclass(frozen=True, slots=True)
class TickResult:
    snapshot: Snapshot
    step: Step
    routed: Routed
    muted: tuple[ActiveMute, ...] | None = None


class Daemon:
    def __init__(
        self,
        *,
        source: SignalSource,
        engine: Engine,
        router: Router,
        store: Store,
        notifiers: NotifierHub,
        clock: Clock,
        interval: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        on_loop: Callable[[], None] | None = None,
        on_tick: Callable[[TickResult], None] | None = None,
        notify_timeout: float = NOTIFY_TIMEOUT_S,
        save_router: Callable[[str], None] | None = None,
        muted: Callable[[], tuple[ActiveMute, ...]] | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("poll interval must be positive")
        self._muted = muted
        self._save_router = save_router
        self._source = source
        self._engine = engine
        self._router = router
        self._store = store
        self._notifiers = notifiers
        self._clock = clock  # the same monotonic clock the engine and the collector use
        self._interval = interval
        self._sleep = sleep
        self._on_loop = on_loop
        self._on_tick = on_tick
        self._notify_timeout = notify_timeout
        self._suspended = False

    @property
    def suspended(self) -> bool:
        return self._suspended

    def firing(self) -> int:
        """How many alerts are open right now."""
        return sum(1 for state in self._engine.states().values() if state is State.FIRING)

    # -- sleep ------------------------------------------------------------------------------

    def on_suspend(self) -> None:
        self._store.events.pause()
        self._suspended = True
        self._engine.suspend()

    def on_resume(self) -> None:
        self._engine.resume()  # every rule warms up again; an open alert stays open
        self._source.reset_baselines()  # counters moved while we slept
        self._suspended = False

    # -- one poll ---------------------------------------------------------------------------

    async def tick(self) -> TickResult | None:
        """One poll. ``None`` while suspended."""
        if self._suspended:
            return None
        snapshot = self._source.collect()
        self._store.events.observe(self._clock(), 3 * self._interval)
        step = self._engine.evaluate(snapshot.signals)
        self._store.record(snapshot.metrics, step)  # the signals reached it via the journal
        rules = {rule.id: rule for rule in self._engine.rules}
        transitions = self._router.route(step.transitions, rules, defer_delivery=True)
        released = self._router.poll(
            self._clock(), rules, defer_delivery=True, skip=transitions.delivered
        )
        candidates = transitions.delivered + released.delivered
        delivered: list[Notification] = []
        self._notifiers.discard_closed(self._router.open_alerts())
        if candidates:
            try:
                async with asyncio.timeout(self._notify_timeout):
                    for note in candidates:
                        if await self._notifiers.send([note]):
                            self._router.confirm_delivery(note, at=self._clock())
                            delivered.append(note)
            except TimeoutError:
                log.warning("notifiers took longer than %s s; moving on", self._notify_timeout)
        routed = Routed(
            tuple(delivered),
            transitions.held
            + released.held
            + tuple(Held(n, "delivery failed") for n in candidates if n not in delivered),
        )
        self._store.events.record(routed)
        self._store.events.sample(step.evaluations, rules, snapshot.units)
        if self._save_router is not None:
            self._save_router(self._router.checkpoint())
        result = TickResult(snapshot, step, routed, self._muted() if self._muted else None)
        if self._on_tick is not None:
            self._on_tick(result)
        return result

    # -- the loop ---------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Poll every ``interval`` seconds until ``stop`` is set, then flush the store.

        Ticks are scheduled against a deadline, so slow polls don't drift. A poll that overruns
        its slot is not made up for with a burst: the schedule restarts from now.
        """
        deadline = self._clock()
        failures = 0
        try:
            while not stop.is_set():
                if self._on_loop is not None:
                    self._on_loop()
                try:
                    await self.tick()
                    failures = 0
                except Exception:
                    failures += 1
                    if failures % LOG_EVERY == 1:
                        log.exception("poll failed (%d in a row)", failures)
                deadline += self._interval
                now = self._clock()
                if deadline < now:
                    deadline = now
                await self._wait(max(deadline - now, 0.0), stop)
        finally:
            self._store.flush()

    async def _wait(self, delay: float, stop: asyncio.Event) -> None:
        if self._sleep is not None:
            await self._sleep(delay)
            return
        with contextlib.suppress(TimeoutError):  # the interval passed: poll again
            await asyncio.wait_for(stop.wait(), delay)
