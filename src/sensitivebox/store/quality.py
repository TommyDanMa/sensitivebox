"""Local ISO weeks and persisted per-rule quality measurements."""

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def local_zone() -> tzinfo:
    name = os.environ.get("TZ", "").removeprefix(":")
    if not name:
        name = str(Path("/etc/localtime").resolve()).partition("/zoneinfo/")[2]
    try:
        return ZoneInfo(name or "UTC")
    except (ValueError, ZoneInfoNotFoundError):
        return datetime.now().astimezone().tzinfo or ZoneInfo("UTC")


def week_at(ts: float, zone: tzinfo) -> tuple[str, str]:
    local = datetime.fromtimestamp(ts, zone)
    year, week, _ = local.isocalendar()
    return f"{year}-W{week:02}", str(zone)


def week_spans(start: float, end: float, zone: tzinfo) -> list[tuple[str, str, float]]:
    spans: list[tuple[str, str, float]] = []
    while start < end:
        local = datetime.fromtimestamp(start, zone)
        monday = (local + timedelta(days=7 - local.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        stop = min(end, monday.timestamp())
        week, name = week_at(start, zone)
        spans.append((week, name, stop - start))
        start = stop
    return spans


@dataclass(frozen=True, slots=True)
class Weekly:
    rule_id: str
    week: str
    zone: str
    fires: int
    firing_minutes: float
    median_duration_s: float | None
    percent_closed_under_5m: float | None
    percent_acknowledged: float | None
    percent_muted: float | None
    incomplete_durations: int
