"""Wi-Fi signal: ``/proc/net/wireless`` first, NetworkManager as the fallback.

Kernels without wireless extensions for Wi-Fi 7 cards (MT7925 on 7.0, for example) list no
interfaces in ``/proc/net/wireless``, so ``wifi_nm`` asks NetworkManager instead. Having no
Wi-Fi, or no NetworkManager, is a normal *unavailable* state with a reason, never an error.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from sensitivebox.hwmon.reader import Opener, default_opener, read_attr

SOURCE_PROC = "/proc/net/wireless"
SOURCE_NM = "NetworkManager"

# Link quality is reported out of a driver-specific maximum that only an ioctl reveals;
# cfg80211 (every modern driver) uses 70.
_WEXT_QUALITY_MAX = 70.0


@dataclass(frozen=True, slots=True)
class WifiSignal:
    iface: str
    percent: int  # 0 to 100
    dbm: int | None  # only /proc/net/wireless reports it
    source: str  # SOURCE_PROC or SOURCE_NM

    @property
    def label(self) -> str:
        return f"Signal {self.percent} %"


@dataclass(frozen=True, slots=True)
class WifiStatus:
    """Exactly one of ``signal`` and ``reason`` is set."""

    signal: WifiSignal | None = None
    reason: str | None = None

    @classmethod
    def ok(cls, signal: WifiSignal) -> "WifiStatus":
        return cls(signal=signal)

    @classmethod
    def unavailable(cls, reason: str) -> "WifiStatus":
        return cls(reason=reason)

    @property
    def available(self) -> bool:
        return self.signal is not None


def _number(text: str) -> float | None:
    try:
        return float(text.rstrip("."))
    except ValueError:
        return None


def parse_proc_wireless(text: str) -> dict[str, WifiSignal]:
    """Two header lines, then ``iface: status link level noise …`` (values may end in ``.``)."""
    out: dict[str, WifiSignal] = {}
    for line in text.splitlines()[2:]:
        iface, sep, rest = line.partition(":")
        fields = rest.split()
        if not sep or len(fields) < 3:
            continue
        link, level = _number(fields[1]), _number(fields[2])
        if link is None:
            continue
        percent = round(min(max(link / _WEXT_QUALITY_MAX, 0.0), 1.0) * 100)
        dbm = int(level) if level is not None and level < 0 else None
        name = iface.strip()
        out[name] = WifiSignal(name, percent, dbm, SOURCE_PROC)
    return out


def read_proc_wireless(proc_root: Path, opener: Opener = default_opener) -> dict[str, WifiSignal]:
    text = read_attr(proc_root / "net" / "wireless", opener)
    return parse_proc_wireless(text) if text else {}


def choose(proc: Mapping[str, WifiSignal], nm: WifiStatus | None) -> WifiStatus:
    """Prefer ``/proc/net/wireless``; otherwise NetworkManager's answer (``None`` = not asked)."""
    if proc:
        return WifiStatus.ok(proc[min(proc)])
    if nm is not None:
        return nm
    return WifiStatus.unavailable("no entry in /proc/net/wireless; NetworkManager not asked")
