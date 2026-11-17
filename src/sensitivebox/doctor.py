"""Render ``sensitivebox doctor`` output: every chip and sensor, for bug reports.

Unavailable and junk sensors are listed too (the dashboard hides them), because a bug report needs
to show exactly what the kernel exposed. Both renderers return redacted text (``redact``): it goes
into public bug reports.
"""

from collections.abc import Mapping, Sequence

from sensitivebox.alerts import Rule
from sensitivebox.hwmon import Chip, Group, Kind, Reading, ReadStatus, Sensor
from sensitivebox.redact import device_label, redact
from sensitivebox.sources import SourcesReport
from sensitivebox.sources.psi import PsiLine

_HEADERS = ("GROUP", "ID", "LABEL", "KIND", "VALUE", "LIMITS", "DEFAULT RULES", "STATUS")
_GROUP_ORDER = {group: i for i, group in enumerate(Group)}
_STATUS_TEXT = {
    ReadStatus.OK: "ok",
    ReadStatus.UNAVAILABLE: "unavailable",
    ReadStatus.JUNK: "junk",
}


def format_value(kind: Kind, value: float) -> str:
    match kind:
        case Kind.TEMP:
            return f"{value:.1f} °C"
        case Kind.FAN:
            return f"{value:.0f} RPM"
        case Kind.PWM:
            return f"{value:.0f} %"
        case Kind.IN:
            return f"{value:.2f} V"
        case Kind.CURR:
            return f"{value:.3f} A"
        case Kind.POWER:
            return f"{value:.2f} W"
        case Kind.FREQ:
            return f"{value / 1e6:.0f} MHz" if value >= 1e6 else f"{value:.0f} Hz"


def _format_limits(sensor: Sensor) -> str:
    """Limits share the VALUE column's unit, so it's left off: ``max 86.8, crit 89.8``."""
    limits = sensor.limits
    parts = [
        f"{name} {format_value(sensor.kind, value).rsplit(' ', 1)[0]}"
        for name, value in (("min", limits.min), ("max", limits.max), ("crit", limits.crit))
        if value is not None
    ]
    if limits.alarm:
        parts.append("ALARM")
    return ", ".join(parts) or "-"


def _rules_for(sensor: Sensor, rules: Sequence[Rule]) -> str:
    """Rule ids watching this sensor; ``-`` means none, so the dashboard shows it grey."""
    return ",".join(r.id for r in rules if r.matches(sensor.id)) or "-"


def _row(sensor: Sensor, reading: Reading | None, rules: Sequence[Rule]) -> tuple[str, ...]:
    status = reading.status if reading else ReadStatus.UNAVAILABLE
    if reading and reading.value is not None:
        value = format_value(sensor.kind, reading.value)
    elif reading and reading.raw is not None:
        value = f"raw {reading.raw}"
    else:
        value = "N/A"
    status_text = _STATUS_TEXT[status]
    if status is ReadStatus.UNAVAILABLE:
        status_text += f" ({reading.reason if reading and reading.reason else 'not read'})"
    return (
        sensor.group.value,
        sensor.id,
        sensor.label,
        sensor.kind.value,
        value,
        _format_limits(sensor),
        _rules_for(sensor, rules),
        status_text,
    )


def _table(rows: Sequence[tuple[str, ...]], *, header: bool = False, indent: str = "") -> list[str]:
    """Left-aligned columns, two spaces apart; ``header`` underlines the first row."""
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines: list[str] = []
    for n, row in enumerate(rows):
        cells = "  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True))
        lines.append((indent + cells).rstrip())
        if header and n == 0:
            lines.append(indent + "  ".join("-" * w for w in widths))
    return lines


def render(
    chips: Sequence[Chip],
    readings: Mapping[str, Reading],
    *,
    verbose: bool = False,
    rules: Sequence[Rule] = (),
) -> str:
    """The doctor report. ``verbose`` lists each chip without sensors on its own line, with its
    path; otherwise they're named in a single summary line. ``rules`` fills the RULES column."""
    # Stable sort: within a group, keep discovery order (chips by name, channels naturally).
    sensors = sorted(
        (s for chip in chips for s in chip.sensors),
        key=lambda s: _GROUP_ORDER[s.group],
    )
    lines: list[str] = []
    if sensors:
        rows = [_HEADERS, *(_row(s, readings.get(s.id), rules) for s in sensors)]
        lines += _table(rows, header=True)
    else:
        lines.append("No hwmon sensors found.")

    counts = {status: 0 for status in ReadStatus}
    for s in sensors:
        reading = readings.get(s.id)
        counts[reading.status if reading else ReadStatus.UNAVAILABLE] += 1
    lines += [
        "",
        f"{len(chips)} chips, {len(sensors)} sensors: {counts[ReadStatus.OK]} ok, "
        f"{counts[ReadStatus.UNAVAILABLE]} unavailable, {counts[ReadStatus.JUNK]} junk"
        " (unavailable and junk sensors are hidden from the dashboard).",
    ]

    empty = [c for c in chips if not c.sensors]
    if empty and verbose:
        lines.append("Chips with no sensors:")
        lines += _table([(c.name, c.bus_id, str(c.path)) for c in empty], indent="  ")
    elif empty:
        names = ", ".join(c.name for c in empty)
        lines.append(f"Chips with no sensors (hidden, --verbose for details): {names}")

    dropped = [
        f"{s.id} {name} {value:.2f} {s.unit}" for s in sensors for name, value in s.dropped_limits
    ]
    if dropped:
        lines.append("Dropped junk limits:")
        lines += [f"  {entry}" for entry in dropped]
    return redact("\n".join(lines))


# -- System sources -------------------------------------------------------------------------

NOT_HERE = "not available on this box"


def format_bytes(n: float) -> str:
    """Decimal units, as the dashboard shows them: ``2.4 GB``."""
    for unit in ("B", "kB", "MB", "GB"):
        if abs(n) < 1000:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


_PSI_W = 20


def _pct(fraction: float, digits: int = 0) -> str:
    return f"{fraction * 100:.{digits}f} %"


def _psi(line: PsiLine | None) -> str:
    return "  -  /  -  " if line is None else f"{line.avg10:5.2f} / {line.avg60:5.2f}"


def _memory_rows(report: SourcesReport) -> list[tuple[str, str]]:
    mem = report.meminfo
    if mem is None:
        return [("Memory", NOT_HERE), ("Swap", NOT_HERE)]
    return [
        (
            "Memory",
            f"{format_bytes(mem.used)} of {format_bytes(mem.total)} used "
            f"({_pct(mem.used_fraction)}), {format_bytes(mem.available)} available; "
            f"cached {format_bytes(mem.cached)}, dirty {format_bytes(mem.dirty)}",
        ),
        (
            "Swap",
            f"{format_bytes(mem.swap_used)} of {format_bytes(mem.swap_total)} used"
            if mem.swap_total
            else "none",
        ),
    ]


def _zram_rows(report: SourcesReport) -> list[tuple[str, str]]:
    if not report.zram:
        return [("zram", NOT_HERE)]
    rows: list[tuple[str, str]] = []
    for n, z in enumerate(report.zram):
        text = (
            f"{z.name}  {z.ratio:.1f}\N{MULTIPLICATION SIGN}, {format_bytes(z.saved_bytes)} saved "
            f"({format_bytes(z.orig_bytes)} stored in {format_bytes(z.mem_used_bytes)})"
            if z.ratio is not None
            else f"{z.name}  empty"
        )
        rows.append(("zram" if n == 0 else "", text))
    return rows


def _pressure_rows(report: SourcesReport) -> list[tuple[str, str]]:
    if not report.pressure:
        return [("Pressure", f"{NOT_HERE} (PSI off)")]
    rows = [("Pressure", f"{'':<6}  {'some avg10 / avg60 %':<{_PSI_W}}  full avg10 / avg60 %")]
    for name, p in report.pressure.items():
        trend = "  rising" if p.some.rising else ""
        rows.append(("", f"{name:<6}  {_psi(p.some):<{_PSI_W}}  {_psi(p.full)}{trend}"))
    return rows


def _vm_row(report: SourcesReport) -> tuple[str, str]:
    vm = report.vmstat
    if vm is None:
        return ("VM", NOT_HERE)

    def n(value: int | None) -> str:
        return "n/a" if value is None else f"{value:,}"

    return (
        "VM",
        f"{n(vm.oom_kill)} OOM kills; swapped in {n(vm.pswpin)} pages, "
        f"out {n(vm.pswpout)} pages (since boot)",
    )


def _ghz(hz: float) -> str:
    return f"{hz / 1e9:.2f}"


def _cpu_row(report: SourcesReport) -> tuple[str, str]:
    parts: list[str] = []
    count = len(report.cpu_times.cores) if report.cpu_times else len(report.cpu_freqs)
    if count:
        parts.append(f"{count} cores")
    current = [f.cur_hz for f in report.cpu_freqs if f.cur_hz is not None]
    if current:
        avg = sum(current) / len(current)
        span = f"{_ghz(min(current))}\N{EN DASH}{_ghz(max(current))}"
        parts.append(f"now {span} GHz (avg {_ghz(avg)})")
    ceilings = [f.max_hz for f in report.cpu_freqs if f.max_hz is not None]
    if ceilings:
        parts.append(f"max {_ghz(max(ceilings))} GHz")
    else:
        parts.append("clock speed not available")
    if report.cpu_times:
        parts.append(f"{_pct(report.cpu_times.busy_since_boot, 1)} busy since boot")
    return ("CPU", "; ".join(parts) if count else NOT_HERE)


def _net_rows(report: SourcesReport) -> list[tuple[str, str]]:
    if not report.net:
        return [("Network", "no interfaces (lo, veth*, docker*, br-* are skipped)")]
    width = max(len(name) for name in report.net)
    return [
        (
            "Network" if n == 0 else "",
            f"{c.iface:<{width}}  {c.operstate or '?':<7}  ↓ {format_bytes(c.rx_bytes)}  "
            f"↑ {format_bytes(c.tx_bytes)} since boot{'  (virtual)' if c.virtual else ''}",
        )
        for n, c in enumerate(report.net.values())
    ]


def _wifi_row(report: SourcesReport) -> tuple[str, str]:
    signal = report.wifi.signal
    if signal is None:
        return ("Wi-Fi", f"unavailable ({report.wifi.reason})")
    dbm = f", {signal.dbm} dBm" if signal.dbm is not None else ""
    return ("Wi-Fi", f"{signal.iface}  {signal.label}{dbm}  (source: {signal.source})")


def _bluetooth_rows(report: SourcesReport) -> list[tuple[str, str]]:
    bt = report.bluetooth
    if bt is None:
        return [("Bluetooth", "not queried")]
    if not bt.available:
        return [("Bluetooth", f"unavailable ({bt.reason})")]
    rows: list[tuple[str, str]] = []
    for n, adapter in enumerate(bt.adapters):
        devices = [d for d in bt.devices if d.adapter == adapter.name]
        listed = ", ".join(device_label(d) for d in devices)  # never names or addresses
        state = "on" if adapter.powered else "off"
        text = f"{adapter.name}  powered {state}; {len(devices)} connected"
        rows.append(("Bluetooth" if n == 0 else "", f"{text}: {listed}" if listed else text))
    return rows


def _apps_rows(report: SourcesReport) -> list[tuple[str, str]]:
    if report.apps is None:
        used = f"memory {_pct(report.meminfo.used_fraction)}, " if report.meminfo else ""
        return [("Top apps", f"not sampled ({used}memory pressure steady)")]
    if not report.apps:
        return [("Top apps", report.apps_reason or "none found")]
    width = max(len(a.name) for a in report.apps)
    return [
        (
            "Top apps" if n == 0 else "",
            f"{a.name:<{width}}  {format_bytes(a.bytes)}"
            + (f"  ({a.units} units)" if a.units > 1 else ""),
        )
        for n, a in enumerate(report.apps)
    ]


def render_sources(report: SourcesReport) -> str:
    """The "System sources" block printed below the sensor table."""
    rows = [
        *_memory_rows(report),
        *_zram_rows(report),
        *_pressure_rows(report),
        _vm_row(report),
        _cpu_row(report),
        *_net_rows(report),
        _wifi_row(report),
        *_bluetooth_rows(report),
        *_apps_rows(report),
    ]
    return redact("\n".join(["System sources", *_table(rows, indent="  ")]))
