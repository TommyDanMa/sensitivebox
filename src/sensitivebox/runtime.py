"""Assemble the foreground watcher and packaged demos.

Watch reads sysfs/procfs and stores real history. Demo uses packaged or explicitly selected
fixture inputs and scripted readings, with in-memory history and optional desktop popups.
Buses are injected; an explicit machine root never connects host buses. The collector, engine,
store and notifier pipeline are shared by live monitoring and demos.
"""

import asyncio
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from sensitivebox import __version__
from sensitivebox.alerts import Engine, Router, Rule
from sensitivebox.alerts.engine import Clock
from sensitivebox.alerts.mute import PersistentMute, boot_id
from sensitivebox.api import ApiState, create_app
from sensitivebox.api.server import serve_api
from sensitivebox.collector import Collector
from sensitivebox.daemon import Daemon, SignalSource, TickResult
from sensitivebox.fixtures import MachineRoot
from sensitivebox.hwmon.reader import default_opener
from sensitivebox.live import live_status, render_live
from sensitivebox.notify import DesktopNotifier, LogNotifier, Notifier, NotifierHub
from sensitivebox.output import Document, encode
from sensitivebox.redact import device_label, redact
from sensitivebox.simulate import Simulation, fixture_opener, resolve_fixture, scenario_named
from sensitivebox.sources.bluez import BluetoothStatus
from sensitivebox.sources.bluez_watch import BluezWatcher
from sensitivebox.sources.changes import ChangeTracker
from sensitivebox.sources.dbus import Bus
from sensitivebox.sources.logind import LogindWatcher
from sensitivebox.sources.power_profiles import PowerProfileWatcher
from sensitivebox.sources.wifi import choose
from sensitivebox.sources.wifi_nm import NmWifiWatcher
from sensitivebox.store import Store

HEARTBEAT_S = 30.0
MAX_GAP_POLLS = 3  # docs/BIBLE.md, "Time"


@dataclass(frozen=True, slots=True)
class RunOptions:
    sysfs_root: Path
    proc_root: Path
    interval: float
    db: Path | None  # None: in memory
    simulate: str | None = None
    scenario: str = "idle"
    desktop: bool = True  # send desktop notifications
    heartbeat_s: float = HEARTBEAT_S
    root: MachineRoot | None = None
    json: bool = False
    fixture_notifications: bool = False


@dataclass(frozen=True, slots=True)
class Buses:
    system: Bus | None = None  # BlueZ, logind
    session: Bus | None = None  # desktop notifications


def _run_all(hooks: list[Callable[[], None]]) -> None:
    for hook in hooks:
        hook()


def describe_bluetooth(status: BluetoothStatus) -> str:
    if not status.available:
        return f"bluetooth: {status.reason}"
    state = "on" if status.powered else "off"
    devices = ", ".join(device_label(d) for d in status.devices)  # never device names
    return f"bluetooth: {state}" + (f" · {devices}" if devices else " · no devices connected")


async def run_daemon(
    options: RunOptions,
    rules: list[Rule],
    buses: Buses,
    stop: asyncio.Event,
    out: Callable[[str], None] = print,
    *,
    clock: Clock = time.monotonic,
    wall: Clock = time.time,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:

    def say(line: str, kind: str = "message", data: object | None = None) -> None:
        out(
            encode(Document(kind, data if data is not None else line))
            if options.json
            else redact(line)
        )

    simulated = options.simulate is not None
    if options.root is not None:
        sysfs_root, proc_root = options.root.sys, options.root.proc
    elif options.simulate is not None:
        fixture = resolve_fixture(options.simulate)
        sysfs_root, proc_root = fixture.sys, fixture.proc
    else:
        sysfs_root, proc_root = options.sysfs_root, options.proc_root
    collector = Collector(
        sysfs_root,
        proc_root,
        clock=clock,
        poll_interval=options.interval,
        opener=fixture_opener if simulated or options.root is not None else default_opener,
    )
    source: SignalSource = collector
    simulation: Simulation | None = None
    if options.simulate is not None:
        simulation = Simulation(collector, scenario_named(options.scenario), clock)
        source = simulation

    store = Store(options.db, clock=wall)
    engine = Engine(
        rules,
        clock=clock,
        limits=collector.limits,
        journal=store.journal,
        max_gap=MAX_GAP_POLLS * options.interval,  # a longer gap is MISSING, never breach time
    )
    notifiers: list[Notifier] = [
        LogNotifier(say, now=wall, event=lambda n, text: say(text, "alert", n))
    ]
    if options.desktop and buses.session is not None:
        notifiers.append(DesktopNotifier(buses.session))

    hooks: list[Callable[[], None]] = []  # the simulation's, once the daemon exists
    heartbeat = _Heartbeat(
        say,
        wall,
        clock,
        options.heartbeat_s,
        lambda: daemon.firing(),
        emit=lambda kind, data, text: say(text, kind, data),
    )
    try:
        boot = boot_id(proc_root)
    except OSError:
        boot = uuid.uuid4().hex  # unavailable boot identity cannot carry mutes across sessions
    mute = PersistentMute(store.mutes, boot, clock, wall)
    router = Router(mute=mute)
    router_key = f"router:{boot}"
    row = store.mutes.db.execute(
        "SELECT value FROM operations_meta WHERE key=?", (router_key,)
    ).fetchone()
    if row:
        router.restore(row[0])
        engine.restore_alerts(router.open_alerts())

    previous_router = str(row[0]) if row else None

    def save_router(payload: str) -> None:
        nonlocal previous_router
        if payload == previous_router:
            return
        with store.mutes.db:
            store.mutes.db.execute("DELETE FROM operations_meta WHERE key LIKE 'router:%'")
            store.mutes.db.execute("INSERT INTO operations_meta VALUES(?,?)", (router_key, payload))
        previous_router = payload

    bluez = BluezWatcher(lambda status: say(describe_bluetooth(status)))
    wifi = NmWifiWatcher()
    fixture_bluetooth: BluetoothStatus | None = None
    fixture_wifi = None
    api = ApiState(
        store,
        collector,
        engine=engine,
        clock=clock,
        wall=wall,
        boot=boot,
        interval=options.interval,
        bluetooth=lambda: fixture_bluetooth if fixture_bluetooth is not None else bluez.current(),
        wifi=lambda: choose(
            collector.wireless, fixture_wifi if fixture_wifi is not None else wifi.current()
        ),
    )

    def tick(result: TickResult) -> None:
        heartbeat.tick(result)
        api.tick(result)

    daemon = Daemon(
        source=source,
        engine=engine,
        router=router,
        save_router=save_router,
        muted=lambda: store.mutes.active(boot, clock()),
        store=store,
        notifiers=NotifierHub(notifiers),
        clock=clock,
        interval=options.interval,
        sleep=sleep,
        on_loop=lambda: _run_all(hooks),
        on_tick=tick,
    )
    if simulation is not None:
        hooks.append(simulation.loop_hook(daemon.on_suspend, daemon.on_resume))

    apt_root = (
        options.root.path / "var/log/apt"
        if options.root is not None
        else Path("/var/log/apt")
        if not simulated and proc_root == Path("/proc") and sysfs_root == Path("/sys")
        else proc_root.parent / "var/log/apt"
    )
    tracker = ChangeTracker(store.changes, sysfs_root, proc_root, apt_root)
    last_changes = float("-inf")

    def scan_changes() -> None:
        nonlocal last_changes
        if clock() - last_changes >= 60:
            tracker.scan(__version__)
            last_changes = clock()

    hooks.append(scan_changes)
    profiles = PowerProfileWatcher(lambda value: store.changes.observe("power_profile", value))
    logind = LogindWatcher(daemon.on_suspend, daemon.on_resume)
    say(_banner(options, collector, rules, simulation), "startup")
    if options.desktop and buses.session is None:
        say("notifications were not sent: no session bus; start a desktop session for popups")
    try:
        if options.root is not None:
            fixture_bluetooth, fixture_wifi = await options.root.snapshot()
            say(describe_bluetooth(fixture_bluetooth))
        elif not simulated:
            await profiles.start(buses.system)
            await bluez.start(buses.system)
            await wifi.start(buses.system)
            if not await logind.start(buses.system):
                say("sleep/resume: logind not available; evaluation won't pause across sleep")
        os_release = (
            Path("/etc/os-release")
            if not simulated and options.root is None and proc_root == Path("/proc")
            else proc_root.parent / "etc/os-release"
        )
        app = create_app(api, proc_root=proc_root, os_release=os_release)
        async with serve_api(app, say):
            try:
                await daemon.run(stop)
            finally:
                api.close()
    finally:
        await profiles.close()
        await bluez.close()
        await wifi.close()
        await logind.close()
        store.close()
        say("stopped", "stopped")


def _store_label(options: RunOptions) -> str:
    return "memory (simulation)" if options.db is None else str(options.db)


def _banner(
    options: RunOptions,
    collector: Collector,
    rules: list[Rule],
    simulation: Simulation | None,
) -> str:
    lines = [
        f"Sensitive Box {__version__} · {socket.gethostname()} · polling every "
        f"{options.interval:g} s",
        f"{len(collector.limits)} sensors · {len(rules)} rules · store: {_store_label(options)}",
    ]
    if simulation is not None:
        scenario = simulation.scenario
        lines.append(
            f"simulating {options.simulate} · scenario {scenario.name}: {scenario.description}"
        )
    lines.append("Ctrl-C to stop")
    return "\n".join(lines)


class _Heartbeat:
    """One quiet status line every ``every`` seconds, so a silent run is visibly alive."""

    def __init__(
        self,
        out: Callable[[str], None],
        wall: Clock,
        clock: Clock,
        every: float,
        firing: Callable[[], int],
        emit: Callable[[str, object, str], None] | None = None,
    ) -> None:
        self._out, self._wall, self._clock, self._every = out, wall, clock, every
        self._firing = firing
        self._emit = emit
        self._last = clock()

    def tick(self, result: TickResult) -> None:
        now = self._clock()
        if now - self._last < self._every:
            return
        self._last = now
        stamp = time.strftime("%H:%M:%S", time.localtime(self._wall()))
        self._out(
            f"{stamp}  ok       {len(result.snapshot.signals)} signals · {self._firing()} firing"
        )
        view = live_status(result)
        if self._emit is not None:
            self._emit("status", view, render_live(view))
        else:
            self._out(render_live(view))
