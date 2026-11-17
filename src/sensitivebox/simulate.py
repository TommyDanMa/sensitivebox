"""Scripted scenarios for ``sbox demo <scenario>``.

The simulator reads the fixture through the real :class:`~sensitivebox.collector.Collector`, so
every signal has the right name and shape, then replaces some of the values with a script. The
daemon, engine, store and notifiers run unchanged: it is for UI development, demos, screenshots
and the alerting scenario tests, never a second code path.

A scenario is data: *curves* (a signal glob and piecewise-linear points ``(seconds, value)``),
*pulses* (a value for one poll, for event-like signals such as an OOM kill) and *events*
(``suspend``/``resume``, as logind would send them). Seconds count from the start of the run.
Curves only replace signals that have a real value; a MISSING signal stays MISSING.

Every scenario also gives the simulated machine a proper 60 W USB-C charger, so the only alerts
you see are the ones the scenario is about (the fixture's own port is a 7.5 W slow charger).
"""

import errno
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
from itertools import pairwise
from pathlib import Path
from typing import Literal

from sensitivebox.alerts.engine import Clock
from sensitivebox.alerts.model import Missing, Value
from sensitivebox.collector import Collector, Snapshot
from sensitivebox.fixtures import FixtureError, MachineRoot, demo_root
from sensitivebox.hwmon.model import Limits

type Curve = tuple[tuple[float, float], ...]
type EventKind = Literal["suspend", "resume"]

CPU_TEMP = ("k10temp/*/temp1", "coretemp/*/temp1")
GPU_TEMP = ("amdgpu/*/temp1",)
USB_C_POWER = "power_supply/ucsi-source-psy-*/power_w"
PSI_SOME = "psi/memory/some_avg10"
PSI_FULL = "psi/memory/full_avg10"
AVAILABLE = "memory/available_pct"
OOM = "vmstat/oom_kill_delta"

ENODATA_MARKER = "<ENODATA>"


class SimulationError(Exception):
    """Something the user asked for that can't be simulated, in words they can act on."""


def value_at(curve: Curve, t: float) -> float:
    """Piecewise-linear: held at the first value before the first point and at the last after."""
    if t <= curve[0][0]:
        return curve[0][1]
    for (t0, v0), (t1, v1) in pairwise(curve):
        if t <= t1:
            return v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    return curve[-1][1]


@dataclass(frozen=True, slots=True)
class Pulse:
    glob: str
    at: float  # seconds; the value is published on the first poll at or after this
    value: float


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    description: str
    curves: Mapping[str, Curve]
    pulses: tuple[Pulse, ...] = ()
    events: tuple[tuple[float, EventKind], ...] = ()


def _curves(globs: tuple[str, ...], curve: Curve) -> dict[str, Curve]:
    return dict.fromkeys(globs, curve)


GOOD_CHARGER: dict[str, Curve] = {USB_C_POWER: ((0.0, 60.0),)}

# CPU 40 -> 95 degC in a minute, then cooling (the bible's example). cpu-hot fires about 65 s in
# (90 degC held for 10 s) and resolves about 113 s in (below 85 for 10 s).
HEATWAVE_CPU: Curve = ((0, 40), (60, 95), (75, 95), (105, 84))

SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in (
        Scenario("idle", "Fixture values, nothing wrong: no alert fires.", dict(GOOD_CHARGER)),
        Scenario(
            "heatwave",
            "CPU climbs 40 to 95 degC and cools again: 'CPU hot' fires about 65 s in and "
            "resolves about 113 s in. The GPU warms up but stays below its limit.",
            {
                **GOOD_CHARGER,
                **_curves(CPU_TEMP, HEATWAVE_CPU),
                **_curves(GPU_TEMP, ((0, 45), (60, 78), (105, 60))),
            },
        ),
        Scenario(
            "memory-crunch",
            "Memory runs out, then recovers: 'Memory stall' fires about 39 s in, 'Memory "
            "pressure' about 57 s, 'Low memory' about 79 s, an OOM kill is reported at 75 s, "
            "and everything resolves by about 135 s.",
            {
                **GOOD_CHARGER,
                AVAILABLE: ((0, 35), (60, 4), (90, 4), (110, 40)),
                PSI_SOME: ((0, 0), (45, 34), (90, 34), (105, 5)),
                PSI_FULL: ((0, 0), (45, 7.8), (90, 7.8), (105, 0)),
            },
            pulses=(Pulse(OOM, 75.0, 1.0),),
        ),
        Scenario(
            "suspend-resume",
            "The lid closes while 'CPU hot' is open: suspend at 55 s, resume at 75 s. Nothing "
            "fires or says 'no data' while asleep, the open alert is not notified again, and it "
            "resolves about 115 s in once the CPU has cooled.",
            {
                **GOOD_CHARGER,
                **_curves(CPU_TEMP, ((0, 40), (40, 95), (95, 95), (110, 80))),
            },
            events=((55.0, "suspend"), (75.0, "resume")),
        ),
    )
}


class Simulation:
    """A :class:`~sensitivebox.daemon.SignalSource` that plays a scenario over a real collector."""

    def __init__(self, base: Collector, scenario: Scenario, clock: Clock) -> None:
        self._base = base
        self._scenario = scenario
        self._clock = clock
        self._start = clock()
        self._pulsed: set[Pulse] = set()
        self._events_done = 0
        self.limits: dict[str, Limits] = base.limits

    @property
    def scenario(self) -> Scenario:
        return self._scenario

    def elapsed(self) -> float:
        return self._clock() - self._start

    def collect(self) -> Snapshot:
        snapshot = self._base.collect()
        t = self.elapsed()
        signals: dict[str, Value] = dict(snapshot.signals)
        for name, value in snapshot.signals.items():
            if isinstance(value, Missing):
                continue
            for glob, curve in self._scenario.curves.items():
                if fnmatchcase(name, glob):
                    signals[name] = value_at(curve, t)
        for pulse in self._scenario.pulses:
            if t >= pulse.at and pulse not in self._pulsed:
                self._pulsed.add(pulse)
                for name, value in snapshot.signals.items():
                    if fnmatchcase(name, pulse.glob) and not isinstance(value, Missing):
                        signals[name] = pulse.value
        return Snapshot(snapshot.at, signals, snapshot.metrics, snapshot.apps, snapshot.units)

    def reset_baselines(self) -> None:
        self._base.reset_baselines()

    def due_events(self) -> list[EventKind]:
        """Scripted suspend/resume events whose time has come, each returned once."""
        t = self.elapsed()
        due: list[EventKind] = []
        while self._events_done < len(self._scenario.events):
            at, kind = self._scenario.events[self._events_done]
            if t < at:
                break
            due.append(kind)
            self._events_done += 1
        return due

    def loop_hook(
        self, on_suspend: Callable[[], None], on_resume: Callable[[], None]
    ) -> Callable[[], None]:
        """For ``Daemon(on_loop=...)``: fires the scenario's suspend/resume like logind would."""

        def hook() -> None:
            for kind in self.due_events():
                (on_suspend if kind == "suspend" else on_resume)()

        return hook


def scenario_named(name: str) -> Scenario:
    try:
        return SCENARIOS[name]
    except KeyError:
        raise SimulationError(
            f"unknown scenario {name!r}; choose one of: {', '.join(SCENARIOS)}"
        ) from None


def resolve_fixture(name: str) -> MachineRoot:
    """Installed demo data, or an explicitly supplied unified machine root."""
    try:
        if name == "proart-p16":
            root = demo_root()
        elif Path(name).is_dir():
            root = MachineRoot.load(Path(name))
        else:
            raise SimulationError(f"no fixture {name!r}; available: proart-p16")
    except FixtureError as exc:
        raise SimulationError(str(exc)) from exc
    return root


def fixture_opener(path: Path) -> str:
    """Read a fixture file the way the kernel would: a file holding ``<ENODATA>`` fails."""
    text = path.read_text(encoding="utf-8")
    if text.strip() == ENODATA_MARKER:
        raise OSError(errno.ENODATA, os.strerror(errno.ENODATA), str(path))
    return text
