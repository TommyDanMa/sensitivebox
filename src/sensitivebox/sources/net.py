"""Network throughput from ``/sys/class/net/*/statistics`` byte counters.

Speed is Δbytes / Δt between polls. A counter that goes *down* (driver reload, interface
reset) starts a new baseline: no rate for that poll and nothing added to the totals, never a
negative value. Totals are kept per interface per day; persisting them is the store's job, which
hands them back through ``NetMeter.seed`` after a restart.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from fnmatch import fnmatchcase
from pathlib import Path

from sensitivebox.hwmon.reader import Opener, default_opener, read_attr, read_int

DEFAULT_SKIP: tuple[str, ...] = ("lo", "veth*", "docker*", "br-*")


@dataclass(frozen=True, slots=True)
class NetCounters:
    iface: str
    rx_bytes: int
    tx_bytes: int
    virtual: bool = False  # lives under devices/virtual (bridges, tunnels, VPNs)
    operstate: str | None = None  # "up", "down", "unknown", …


def read_net_counters(
    sysfs_root: Path,
    skip: Iterable[str] = DEFAULT_SKIP,
    opener: Opener = default_opener,
) -> dict[str, NetCounters]:
    """Counters for every interface not matching a ``skip`` glob, by name."""
    class_dir = sysfs_root / "class" / "net"
    patterns = tuple(skip)
    try:
        entries = sorted(class_dir.iterdir())
    except OSError:
        return {}
    out: dict[str, NetCounters] = {}
    for entry in entries:
        name = entry.name
        if any(fnmatchcase(name, p) for p in patterns):
            continue
        stats = entry / "statistics"
        rx = read_int(stats / "rx_bytes", opener)
        tx = read_int(stats / "tx_bytes", opener)
        if rx is None or tx is None:
            continue
        out[name] = NetCounters(
            iface=name,
            rx_bytes=rx,
            tx_bytes=tx,
            virtual="/devices/virtual/" in entry.resolve().as_posix(),
            operstate=read_attr(entry / "operstate", opener),
        )
    return out


@dataclass(frozen=True, slots=True)
class NetRate:
    """Bytes per second since the last poll; ``None`` on a new baseline."""

    iface: str
    rx_bps: float | None
    tx_bps: float | None


@dataclass(frozen=True, slots=True)
class DayTotal:
    rx_bytes: int = 0
    tx_bytes: int = 0

    def __add__(self, other: "DayTotal") -> "DayTotal":
        return DayTotal(self.rx_bytes + other.rx_bytes, self.tx_bytes + other.tx_bytes)


@dataclass(frozen=True, slots=True)
class _Last:
    at: float
    rx: int
    tx: int


class NetMeter:
    """Turns successive counter samples into rates and per-day totals. Owns no clock: the caller
    passes a monotonic time (for rates) and the local date (for totals)."""

    def __init__(self) -> None:
        self._last: dict[str, _Last] = {}
        self._totals: dict[tuple[str, date], DayTotal] = {}

    def seed(self, totals: Mapping[tuple[str, date], DayTotal]) -> None:
        """Restore totals saved by the store; later deltas are added on top."""
        for key, total in totals.items():
            self._totals[key] = self._totals.get(key, DayTotal()) + total

    @property
    def totals(self) -> dict[tuple[str, date], DayTotal]:
        return dict(self._totals)

    def day_totals(self, day: date) -> dict[str, DayTotal]:
        return {iface: t for (iface, d), t in sorted(self._totals.items()) if d == day}

    def update(
        self, counters: Mapping[str, NetCounters], *, monotonic: float, day: date
    ) -> dict[str, NetRate]:
        # An interface that disappears loses its baseline, so it starts fresh if it returns.
        for gone in self._last.keys() - counters.keys():
            del self._last[gone]
        rates: dict[str, NetRate] = {}
        for iface, c in counters.items():
            last = self._last.get(iface)
            if last is not None and monotonic <= last.at:
                # No time has passed: keep the baseline and report nothing new.
                rates[iface] = NetRate(iface, None, None)
                continue
            self._last[iface] = _Last(monotonic, c.rx_bytes, c.tx_bytes)
            if last is None or c.rx_bytes < last.rx or c.tx_bytes < last.tx:
                rates[iface] = NetRate(iface, None, None)  # first sample or counter reset
                continue
            d_rx, d_tx = c.rx_bytes - last.rx, c.tx_bytes - last.tx
            dt = monotonic - last.at
            key = (iface, day)
            self._totals[key] = self._totals.get(key, DayTotal()) + DayTotal(d_rx, d_tx)
            rates[iface] = NetRate(iface, d_rx / dt, d_tx / dt)
        return rates
