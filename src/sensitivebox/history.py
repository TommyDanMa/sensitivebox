"""Bounded incident history, event following and rule replay through the live engine."""

import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import msgspec

from sensitivebox.alerts.engine import Evaluation, format_duration
from sensitivebox.alerts.model import parse_duration
from sensitivebox.alerts.replay import replay
from sensitivebox.alerts.routing import Notification
from sensitivebox.output import Output
from sensitivebox.redact import redact
from sensitivebox.store.events import AlertEvent, Events, Incident, IncidentSummary
from sensitivebox.store.quality import Weekly, local_zone
from sensitivebox.store.read import inputs, reader


@dataclass(frozen=True, slots=True)
class IncidentLog:
    incident: Incident
    events: tuple[AlertEvent, ...]
    summary: IncidentSummary | None = None


@dataclass(frozen=True, slots=True)
class LogView:
    incidents: tuple[IncidentLog, ...]
    truncated: bool
    since: float


@dataclass(frozen=True, slots=True)
class RuleReplay:
    rule_id: str
    evaluations: tuple[Evaluation, ...]
    reason: str | None = None


def parse_since(value: str | None, now: float) -> float:
    local = datetime.fromtimestamp(now, local_zone())
    if value is None or value == "yesterday":
        return (
            (local - timedelta(days=1))
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
    try:
        return now - parse_duration(value)
    except ValueError:
        pass
    try:
        date = datetime.fromisoformat(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=local_zone())
        return date.timestamp()
    except ValueError:
        raise ValueError("--since needs a duration, yesterday, ISO date or timestamp") from None


def rule_replay(path: Path, rule: str) -> RuleReplay:
    events = inputs(path)
    result = replay([], events)
    rows = tuple(e for e in result.evaluations if e.rule_id == rule)
    return RuleReplay(rule, rows, None if rows else "no retained evaluations for this rule")


def log_view(path: Path, since: float, limit: int, rule: str | None = None) -> LogView:
    with reader(path) as db:
        if db is None:
            return LogView((), False, since)
        rows = db.execute(
            "SELECT * FROM alert_incidents WHERE (closed IS NULL OR closed>=?)"
            " AND (? IS NULL OR rule_id=?) ORDER BY opened DESC,id DESC LIMIT ?",
            (since, rule, rule, limit + 1),
        ).fetchall()
        result: list[IncidentLog] = []
        truncated = len(rows) > limit
        for row in rows[:limit]:
            incident = Incident(*row)
            # Bound event rows too; only the newest routing facts are needed in the summary.
            events = tuple(
                e
                for e in Events(db, time.time, writer=False).history(
                    0, limit + 1, incident=incident.id
                )
                if e.incident_id == incident.id
            )
            truncated |= len(events) > limit
            summary = db.execute(
                "SELECT name,severity,peak,unit FROM incident_summaries WHERE incident_id=?",
                (incident.id,),
            ).fetchone()
            result.append(
                IncidentLog(
                    incident, events[:limit], None if summary is None else IncidentSummary(*summary)
                )
            )
        return LogView(tuple(result), truncated, since)


def render_log(view: LogView, now: float) -> str:
    today = datetime.fromtimestamp(now, local_zone()).date()
    lines: list[str] = []
    heading = ""
    for item in view.incidents:
        incident = item.incident
        stamp = datetime.fromtimestamp(incident.opened, local_zone())
        label = (
            "Today"
            if stamp.date() == today
            else "Yesterday"
            if stamp.date() == today - timedelta(days=1)
            else stamp.date().isoformat()
        )
        if heading != label:
            lines.append(label)
            heading = label
        reasons = (
            ", ".join(
                dict.fromkeys(
                    "muted (quiet hours)" if e.reason == "quiet hours" else e.reason
                    for e in reversed(item.events)
                )
            )
            or "recorded"
        )
        status = (
            "open"
            if incident.closed is None
            else f"resolved {datetime.fromtimestamp(incident.closed, local_zone()):%H:%M}"
        )
        summary = item.summary
        severity = (
            summary.severity
            if summary is not None
            else item.events[0].notification.severity
            if item.events
            else "unknown"
        )
        name = summary.name if summary is not None else incident.rule_id
        peak = (
            f"{summary.peak:g} {summary.unit}".rstrip()
            if summary is not None and summary.peak is not None
            else "unavailable"
        )
        duration = (
            format_duration(incident.active_s)
            .replace(" min", "m")
            .replace(" s", "s")
            .replace(" h", "h")
        )
        incomplete = " · duration incomplete" if incident.incomplete else ""
        lines.append(
            f"  {stamp:%H:%M}  {severity.upper():8}  {name:16}  firing {duration}"
            f"   peak {peak} · {status} · {reasons}{incomplete}"
        )
    if view.truncated:
        lines.append("Truncated at --limit; narrow --since or raise --limit.")
    return "\n".join(lines) or "No alerts in this period."


def follow(
    path: Path,
    since: float,
    limit: int,
    rule: str | None,
    output: Output,
    *,
    wait: Callable[[float], bool] | None = None,
) -> None:
    wait = wait or threading.Event().wait
    cursor = 0
    while True:
        with reader(path) as db:
            if db is not None:
                rows = db.execute(
                    "SELECT id,incident_id,ts,kind,reason,notification FROM alert_events"
                    " WHERE id>? AND ts>=? AND (? IS NULL OR incident_id IN"
                    " (SELECT id FROM alert_incidents WHERE rule_id=?)) ORDER BY id LIMIT ?",
                    (cursor, since, rule, rule, limit + 1),
                ).fetchall()

                for row in rows[:limit]:
                    event = AlertEvent(
                        row[0],
                        row[1],
                        row[2],
                        row[3],
                        row[4],
                        msgspec.json.decode(row[5], type=Notification),
                    )
                    cursor = event.id
                    output.emit(
                        "alert_event",
                        event,
                        f"{event.kind} · {event.notification.rule_id}"
                        f" · {event.reason} · {event.notification.body}",
                    )
                if len(rows) > limit:
                    output.emit("truncated", True, "Truncated batch at --limit; continuing.")
                    continue
        if wait(0.5):
            return


def journal(since: str | None, limit: int, following: bool, output: Output) -> int:
    args = [
        "journalctl",
        "--user",
        "--unit=sensitivebox.service",
        "--no-pager",
        f"--lines={limit}",
        "--output=short-iso",
    ]
    stamp = datetime.fromtimestamp(parse_since(since, time.time()), local_zone())
    args += ["--since", stamp.isoformat(sep=" ")]
    if following:
        args.append("--follow")
    if not following:
        try:
            result = subprocess.run(  # noqa: S603 - fixed executable, no shell
                args,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise OSError("journalctl timed out") from None
        if result.returncode:
            raise OSError(redact(result.stderr or "journalctl failed"))
        for line in result.stdout.splitlines():
            output.emit("journal", line, line)
        return 0
    process = subprocess.Popen(  # noqa: S603 - fixed executable, no shell
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        if process.stdout is None:
            raise OSError("journal pipe unavailable")
        for line in process.stdout:
            output.emit("journal", line.rstrip(), line.rstrip())
        code = process.wait(timeout=2)
        if code:
            raise OSError(f"journalctl failed ({code})")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if process.stdout:
            process.stdout.close()
    return 0


def render_weekly(rows: list[Weekly], truncated: bool = False) -> str:
    if not rows:
        return "No weekly alert measurements."
    ack = any(row.percent_acknowledged is not None for row in rows)
    header = "Rule  Week  Zone  Fires  Firing min  Median  Closed <5 min  Muted  Incomplete"
    if ack:
        header += "  Acknowledged"
    lines = [header]

    def percentage(value: float | None) -> str:
        return "unavailable" if value is None else f"{value:g} %"

    for row in rows:
        median = (
            "unavailable"
            if row.median_duration_s is None
            else format_duration(row.median_duration_s)
        )
        line = (
            f"{row.rule_id}  {row.week}  {row.zone}  {row.fires}  {row.firing_minutes:g}  "
            f"{median}  {percentage(row.percent_closed_under_5m)}  "
            f"{percentage(row.percent_muted)}  {row.incomplete_durations}"
        )
        if ack:
            line += "  " + percentage(row.percent_acknowledged)
        lines.append(line)
    if not ack:
        lines.append("Acknowledgement percentage unavailable.")
    if truncated:
        lines.append("Truncated at --limit.")
    return "\n".join(lines)
