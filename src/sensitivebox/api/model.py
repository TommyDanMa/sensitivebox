"""Route-specific, typed wire models. No CLI envelope and no raw hardware paths."""

from dataclasses import dataclass

import msgspec

from sensitivebox.alerts.model import Severity, State, When
from sensitivebox.hwmon.model import Limits
from sensitivebox.sources.wifi import WifiStatus
from sensitivebox.store.events import Incident


@dataclass(frozen=True, slots=True)
class EvaluationView:
    rule_id: str
    state: State
    when: When
    value: float | None
    clear: float | None
    severity: Severity


@dataclass(frozen=True, slots=True)
class SignalView:
    id: str
    label: str
    unit: str
    limits: Limits
    value: float | str | None
    missing: str | None
    evaluations: tuple[EvaluationView, ...]


@dataclass(frozen=True, slots=True)
class AppView:
    name: str
    bytes: int


@dataclass(frozen=True, slots=True)
class BluetoothView:
    powered: bool | None
    devices: tuple[str, ...]
    missing: str | None


@dataclass(frozen=True, slots=True)
class SensorsView:
    timestamp: float
    fresh: bool
    groups: dict[str, tuple[SignalView, ...]]
    apps: tuple[AppView, ...]
    bluetooth: BluetoothView
    wifi: WifiStatus


@dataclass(frozen=True, slots=True)
class HostView:
    hostname: str | None
    ubuntu_release: str | None
    kernel: str | None
    version: str


@dataclass(frozen=True, slots=True)
class IncidentView(Incident):
    name: str | None
    severity: str | None
    peak: float | None
    unit: str | None


@dataclass(frozen=True, slots=True)
class AlertsView:
    incidents: tuple[IncidentView, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class ErrorView:
    error: str


class AckBody(msgspec.Struct, forbid_unknown_fields=True):
    pass


class MuteBody(msgspec.Struct, forbid_unknown_fields=True):
    duration: str
    renotify: bool = True
