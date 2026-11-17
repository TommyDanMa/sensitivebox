"""Scan ``<sysfs_root>/class/hwmon`` once, and read sensor values on every poll."""

from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from sensitivebox.hwmon.classify import group_for, select_inputs
from sensitivebox.hwmon.ids import bus_id, sensor_id
from sensitivebox.hwmon.model import Chip, Kind, Limits, Reading, ReadStatus, Sensor
from sensitivebox.hwmon.normalise import UNIT, is_junk, to_si
from sensitivebox.hwmon.reader import Opener, default_opener, read_attr, read_int, read_raw

_LIMIT_NAMES = ("min", "max", "crit")


def discover(sysfs_root: Path, *, opener: Opener = default_opener) -> list[Chip]:
    """Find every hwmon chip and its sensors. Raises ``FileNotFoundError`` without hwmon."""
    class_dir = sysfs_root / "class" / "hwmon"
    if not class_dir.is_dir():
        raise FileNotFoundError(f"no hwmon class directory at {class_dir}")

    named: list[tuple[Path, str, str]] = []
    for entry in class_dir.iterdir():
        try:
            hwmon_dir = entry.resolve()
            name = read_attr(hwmon_dir / "name", opener)
            if name:
                named.append((hwmon_dir, name, bus_id(hwmon_dir)))
        except OSError:
            continue  # unplugged while we looked, or unreadable: the other chips still count

    by_key: dict[tuple[str, str], list[Chip]] = {}
    for hwmon_dir, name, bus in named:
        try:
            chip = _scan_chip(hwmon_dir, name, bus, opener)
        except OSError:
            continue  # hot-unplugged between reading its name and scanning it (ADV-007)
        by_key.setdefault((name, bus), []).append(chip)

    chips: list[Chip] = []
    for siblings in by_key.values():
        # Same chip name on the same device (rare): number them by what they measure, which is
        # stable, and fall back to the path only when even that is identical (ADV-008).
        siblings.sort(key=lambda c: (_fingerprint(c), str(c.path)))
        chips += [
            c if n == 1 else _with_bus(c, f"{c.bus_id}~{n}") for n, c in enumerate(siblings, 1)
        ]
    return sorted(chips, key=lambda c: (c.name.lower(), c.bus_id))


def _fingerprint(chip: Chip) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((s.channel, s.label) for s in chip.sensors))


def _with_bus(chip: Chip, bus: str) -> Chip:
    sensors = tuple(
        replace(s, bus_id=bus, id=sensor_id(chip.name, bus, s.channel)) for s in chip.sensors
    )
    return replace(chip, bus_id=bus, sensors=sensors)


def _scan_chip(hwmon_dir: Path, name: str, bus: str, opener: Opener) -> Chip:
    files = {p.name for p in hwmon_dir.iterdir() if p.is_file() and not p.is_symlink()}
    sensors = tuple(
        _make_sensor(hwmon_dir, files, name, bus, kind, channel, filename, opener)
        for kind, channel, filename in select_inputs(files)
    )
    return Chip(name=name, bus_id=bus, path=hwmon_dir, sensors=sensors)


def _make_sensor(
    hwmon_dir: Path,
    files: set[str],
    chip: str,
    bus: str,
    kind: Kind,
    channel: str,
    filename: str,
    opener: Opener,
) -> Sensor:
    label_file = f"{channel}_label"
    label = read_attr(hwmon_dir / label_file, opener) if label_file in files else None
    limits, dropped = _read_limits(hwmon_dir, files, kind, channel, opener)
    return Sensor(
        id=sensor_id(chip, bus, channel),
        chip=chip,
        bus_id=bus,
        channel=channel,
        kind=kind,
        label=label or channel,
        group=group_for(chip, kind),
        unit=UNIT[kind],
        input_path=hwmon_dir / filename,
        limits=limits,
        dropped_limits=dropped,
    )


def _read_limits(
    hwmon_dir: Path, files: set[str], kind: Kind, channel: str, opener: Opener
) -> tuple[Limits, tuple[tuple[str, float], ...]]:
    values: dict[str, float | None] = dict.fromkeys(_LIMIT_NAMES)
    dropped: list[tuple[str, float]] = []
    for limit in _LIMIT_NAMES:
        filename = f"{channel}_{limit}"
        if filename not in files:
            continue
        raw = read_int(hwmon_dir / filename, opener)
        if raw is None:
            continue
        value = to_si(kind, raw)
        if is_junk(kind, value):
            dropped.append((limit, value))
        else:
            values[limit] = value

    alarm: bool | None = None
    if f"{channel}_alarm" in files:
        raw_alarm = read_int(hwmon_dir / f"{channel}_alarm", opener)
        alarm = None if raw_alarm is None else raw_alarm != 0

    limits = Limits(min=values["min"], max=values["max"], crit=values["crit"], alarm=alarm)
    return limits, tuple(dropped)


def read(sensor: Sensor, opener: Opener = default_opener) -> Reading:
    """Read one sensor now. One small file read; never spawns a process."""
    result = read_raw(sensor.input_path, opener)
    raw = result.value
    if raw is None:
        return Reading(sensor.id, None, ReadStatus.UNAVAILABLE, reason=result.reason)
    value = to_si(sensor.kind, raw)
    if is_junk(sensor.kind, value):
        return Reading(sensor.id, None, ReadStatus.JUNK, raw)
    return Reading(sensor.id, value, ReadStatus.OK, raw)


def read_all(chips: Iterable[Chip], opener: Opener = default_opener) -> dict[str, Reading]:
    return {s.id: read(s, opener) for chip in chips for s in chip.sensors}
