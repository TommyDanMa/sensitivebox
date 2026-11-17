"""The SQLite store: a 1 h raw ring of input frames plus 1-minute and 15-minute rollups, in WAL
mode.

The alert engine reports every input event to ``journal`` (each poll's signals, rule and limit
changes, suspend and resume) and the daemon calls ``record`` once per poll with the extra metrics
and the rule evaluations. Both only touch memory; every ``FLUSH_EVERY_S`` the batch is written in
one transaction, and about once a minute the store prunes. ``inputs`` reads the input events back
for ``replay``. The wall clock is injected, so retention is tested without waiting. The
connection is used from the daemon's thread only.

A replay of the ring begins at one checkpoint advanced by the pruner. Session restart events
reproduce the fresh engine's Warming reset. There is no checkpoint per poll.
"""

import logging
import math
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

import msgspec

from sensitivebox.alerts.engine import Clock, Evaluation, Step
from sensitivebox.alerts.events import (
    Checkpoint,
    EngineSettings,
    InputEvent,
    LimitsChanged,
    OpenAlerts,
    Restart,
    Resume,
    RulesLoaded,
    Suspend,
    Tick,
)
from sensitivebox.alerts.model import Missing, Rule, Value
from sensitivebox.alerts.replay import ReplayRunner
from sensitivebox.hwmon import Limits
from sensitivebox.store import schema
from sensitivebox.store.changes import Changes
from sensitivebox.store.events import Events
from sensitivebox.store.mutes import Mutes

log = logging.getLogger(__name__)

FLUSH_EVERY_S = 5.0
PRUNE_EVERY_S = 60.0

# frame, ts (wall), mono (engine clock), kind, signal, value, text, missing, payload
type _Input = tuple[
    int, float, float, str, str | None, float | None, str | None, str | None, str | None
]
type _Raw = tuple[int, float, str, str | None, float | None, str | None, str | None, str | None]

_RULES = msgspec.json.Decoder(list[Rule])
_LIMITS = msgspec.json.Decoder(dict[str, Limits])


@dataclass(slots=True)
class _Acc:
    """Count, sum, min and max of the values seen in one bucket since the last flush."""

    n: int
    total: float
    lo: float
    hi: float

    def add(self, value: float) -> None:
        self.n += 1
        self.total += value
        self.lo = min(self.lo, value)
        self.hi = max(self.hi, value)


def _split(value: Value | None) -> tuple[float | None, str | None, str | None]:
    """A signal value as the (number, text, missing reason) columns."""
    if isinstance(value, Missing):
        return None, None, value.reason
    if isinstance(value, str):
        return None, value, None
    return value, None, None


def _join(number: float | None, text: str | None, missing: str | None) -> Value:
    if missing is not None:
        return Missing(missing)
    if text is not None:
        return text
    return number if number is not None else Missing("not recorded")


class Store:
    """Open (creating if needed) the database at ``path``; ``None`` keeps it in memory."""

    def __init__(self, path: Path | None, *, clock: Clock, start_session: bool = True) -> None:
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._db = sqlite3.connect(":memory:" if path is None else str(path))
        for pragma in schema.PRAGMAS:
            self._db.execute(pragma)
        with self._db:
            version = int(self._db.execute(schema.GET_VERSION).fetchone()[0])
            if version == 1:
                for statement in schema.UPGRADE_FROM_1:
                    self._db.execute(statement)
            for statement in schema.CREATE:
                self._db.execute(statement)
            if version == 2:
                for statement in schema.UPGRADE_FROM_2:
                    self._db.execute(statement)
            if version in (2, 3):
                for statement in schema.UPGRADE_FROM_3:
                    self._db.execute(statement)
            self._db.execute(schema.SET_VERSION)
        self.events = Events(self._db, clock, writer=start_session)
        self.mutes = Mutes(self._db)
        self.changes = Changes(self._db, clock)
        self._frame = int(self._db.execute(schema.LAST_FRAME).fetchone()[0])
        checkpoint = self._db.execute("SELECT frame FROM replay_checkpoint").fetchone()
        if checkpoint:
            self._frame = max(self._frame, checkpoint[0] - 1)
        self._ids: dict[str, int] = {}
        self._inputs: list[_Input] = []
        self._tick_wall: float | None = None
        self._metrics: list[tuple[float, str, float]] = []
        self._evaluations: list[tuple[float, Evaluation]] = []
        self._acc_1m: dict[tuple[int, str], _Acc] = {}
        self._acc_15m: dict[tuple[int, str], _Acc] = {}
        now = clock()
        self._last_flush = now
        self._last_prune = now

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def journal_mode(self) -> str:
        row = self._db.execute("PRAGMA journal_mode").fetchone()
        return str(row[0])

    # -- writing ------------------------------------------------------------------------------

    def journal(self, event: InputEvent) -> None:
        """The engine's journal: queue one input event (``Engine(..., journal=store.journal)``).
        Signal values also feed the rollups."""
        now = self._clock()
        self._frame += 1
        frame = self._frame
        match event:
            case Tick(at=at, signals=signals):
                self._tick_wall = now
                self._inputs.append((frame, now, at, "tick", None, None, None, None, None))
                for name, value in signals.items():
                    number, text, missing = _split(value)
                    self._inputs.append(
                        (frame, now, at, "value", name, number, text, missing, None)
                    )
                    if number is not None:
                        self._accumulate(now, name, number)
            case OpenAlerts(at=at, payload=payload):
                self._inputs.append(
                    (frame, now, at, "open_alerts", None, None, None, None, payload)
                )
            case Restart(at=at):
                self._inputs.append((frame, now, at, "restart", None, None, None, None, None))
            case Checkpoint():
                raise ValueError("checkpoint is a replay boundary, not a journal input")
            case Suspend(at=at):
                self._inputs.append((frame, now, at, "suspend", None, None, None, None, None))
            case Resume(at=at):
                self._inputs.append((frame, now, at, "resume", None, None, None, None, None))
            case LimitsChanged(at=at, limits=limits):
                payload = msgspec.json.encode(dict(limits)).decode()
                self._inputs.append((frame, now, at, "limits", None, None, None, None, payload))
            case RulesLoaded(at=at, rules=rules):
                payload = msgspec.json.encode(list(rules)).decode()
                self._inputs.append((frame, now, at, "rules", None, None, None, None, payload))
            case EngineSettings(at=at, max_gap=max_gap):
                self._inputs.append((frame, now, at, "settings", None, max_gap, None, None, None))
        self._maintain(now)

    def record(self, metrics: Mapping[str, float] | None = None, step: Step | None = None) -> None:
        """Queue one poll's extra metrics (stored, never alerted on) and its rule evaluations.
        Flushes and prunes when due, so the caller only has to call this once per poll."""
        now = self._clock() if self._tick_wall is None else self._tick_wall
        self._tick_wall = None
        for name, number in (metrics or {}).items():
            self._metrics.append((now, name, number))
            self._accumulate(now, name, number)
        if step is not None:
            self._evaluations.extend((now, e) for e in step.evaluations)
        self._maintain(now)

    def _maintain(self, now: float) -> None:
        if now - self._last_flush >= FLUSH_EVERY_S:
            self.flush()
        if now - self._last_prune >= PRUNE_EVERY_S:
            self.prune()

    def _accumulate(self, ts: float, name: str, value: float) -> None:
        if not math.isfinite(value):
            return
        for table, width in (
            (self._acc_1m, schema.ROLLUP_1M_WIDTH_S),
            (self._acc_15m, schema.ROLLUP_15M_WIDTH_S),
        ):
            key = (int(ts // width) * width, name)
            acc = table.get(key)
            if acc is None:
                table[key] = _Acc(1, value, value, value)
            else:
                acc.add(value)

    def _signal_id(self, name: str) -> int:
        known = self._ids.get(name)
        if known is not None:
            return known
        self._db.execute(schema.INSERT_SIGNAL, (name,))
        row = self._db.execute(schema.SELECT_SIGNAL, (name,)).fetchone()
        self._ids[name] = int(row[0])
        return self._ids[name]

    def flush(self) -> None:
        """Write everything queued, in one transaction."""
        self._last_flush = self._clock()
        if not (
            self._inputs or self._metrics or self._evaluations or self._acc_1m or self._acc_15m
        ):
            return
        try:
            with self._db:
                self._db.executemany(
                    schema.INSERT_INPUT,
                    [
                        (f, ts, mono, kind, None if n is None else self._signal_id(n), v, t, m, p)
                        for f, ts, mono, kind, n, v, t, m, p in self._inputs
                    ],
                )
                self._db.executemany(
                    schema.INSERT_METRIC,
                    [(ts, self._signal_id(n), v) for ts, n, v in self._metrics],
                )
                self._db.executemany(
                    schema.INSERT_EVALUATION,
                    [self._evaluation_row(ts, e) for ts, e in self._evaluations],
                )
                self._upsert(schema.UPSERT_1M, self._acc_1m)
                self._upsert(schema.UPSERT_15M, self._acc_15m)
        except sqlite3.Error:
            # Disk full, read-only, locked: the daemon keeps alerting; this batch is lost. Ids
            # handed out inside the rolled-back transaction are no longer valid.
            log.exception("store: could not write a batch, dropping it")
            self._ids.clear()
        self._inputs.clear()
        self._metrics.clear()
        self._evaluations.clear()
        self._acc_1m.clear()
        self._acc_15m.clear()

    def _evaluation_row(
        self, ts: float, e: Evaluation
    ) -> tuple[
        float,
        str,
        int,
        float | None,
        str | None,
        str | None,
        str,
        str,
        float | None,
        str | None,
        int,
    ]:
        number, text, _ = _split(e.value)
        return (
            ts,
            e.rule_id,
            self._signal_id(e.signal),
            number,
            text,
            e.missing,
            e.state.value,
            e.reason,
            e.threshold,
            e.alert_severity,
            int(e.no_data),
        )

    def _upsert(self, statement: str, table: Mapping[tuple[int, str], _Acc]) -> None:
        self._db.executemany(
            statement,
            [
                (bucket, self._signal_id(name), a.n, a.total, a.lo, a.hi)
                for (bucket, name), a in table.items()
            ],
        )

    def prune(self) -> None:
        """Drop what is past its retention, as of the injected clock."""
        now = self._clock()
        self._last_prune = now
        self.flush()
        raw_cutoff = now - schema.RAW_RETENTION_S
        try:
            with self._db:
                self._advance_checkpoint(raw_cutoff)
                self.events.prune(now)
                self.changes.prune(now)
                boundary = self._db.execute("SELECT frame FROM replay_checkpoint").fetchone()
                if boundary:
                    self._db.execute(
                        schema.PRUNE_CHECKPOINT_INPUTS,
                        (raw_cutoff, boundary[0], raw_cutoff, raw_cutoff, raw_cutoff),
                    )
                else:
                    self._db.execute(schema.PRUNE_INPUTS, (raw_cutoff,) * 4)
                self._db.execute(schema.PRUNE_METRICS, (raw_cutoff,))
                self._db.execute(schema.PRUNE_EVALUATIONS, (raw_cutoff,))
                self._db.execute(
                    schema.PRUNE_1M, (schema.ROLLUP_1M_WIDTH_S, now - schema.ROLLUP_1M_RETENTION_S)
                )
                self._db.execute(
                    schema.PRUNE_15M,
                    (schema.ROLLUP_15M_WIDTH_S, now - schema.ROLLUP_15M_RETENTION_S),
                )
        except sqlite3.Error:
            log.exception("store: could not prune")

    # -- reading ------------------------------------------------------------------------------

    def inputs(self) -> list[InputEvent]:
        """Every input event in the ring, in the order the engine saw them, ready for
        ``replay``. Flushes first, so nothing queued is left out."""
        self.flush()
        checkpoint = self._db.execute("SELECT frame,mono,payload FROM replay_checkpoint").fetchone()
        rows: list[_Raw] = self._db.execute(schema.SELECT_INPUTS).fetchall()
        if checkpoint is None:
            return self.decode_inputs(rows)
        return [
            Checkpoint(checkpoint[1], checkpoint[2]),
            *self.decode_inputs([r for r in rows if r[0] >= checkpoint[0]]),
        ]

    @property
    def checkpoint_count(self) -> int:
        return int(self._db.execute("SELECT count(*) FROM replay_checkpoint").fetchone()[0])

    def _advance_checkpoint(self, cutoff: float) -> None:
        prior = self._db.execute("SELECT frame,mono,payload FROM replay_checkpoint").fetchone()
        # Legacy hand-written journals have no session identity; preserve their original API.
        if (
            prior is None
            and self._db.execute("SELECT 1 FROM inputs_raw WHERE kind='restart' LIMIT 1").fetchone()
            is None
        ):
            return
        boundary = self._db.execute(
            "SELECT coalesce(min(frame), (SELECT coalesce(max(frame),0)+1 FROM inputs_raw))"
            " FROM inputs_raw WHERE ts>=?",
            (cutoff,),
        ).fetchone()[0]
        runner = ReplayRunner()
        if prior:
            runner.feed(Checkpoint(prior[1], prior[2]))
        rows: list[_Raw] = self._db.execute(schema.SELECT_INPUTS).fetchall()
        for event in self.decode_inputs(
            [r for r in rows if (prior is None or r[0] >= prior[0]) and r[0] < boundary]
        ):
            runner.feed(event)
        self._db.execute(
            "INSERT OR REPLACE INTO replay_checkpoint VALUES(1,?,?,?)",
            (boundary, runner.clock(), runner.engine.checkpoint()),
        )

    @staticmethod
    def decode_inputs(rows: list[_Raw]) -> list[InputEvent]:
        events: list[InputEvent] = []
        tick: tuple[int, float, dict[str, Value]] | None = None
        for frame, mono, kind, name, number, text, missing, payload in rows:
            if tick is not None and (frame != tick[0] or kind != "value"):
                events.append(Tick(tick[1], tick[2]))
                tick = None
            match kind:
                case "tick":
                    tick = (frame, mono, {})
                case "value" if tick is not None and name is not None:
                    tick[2][name] = _join(number, text, missing)
                case "open_alerts" if payload is not None:
                    events.append(OpenAlerts(mono, payload))
                case "restart":
                    events.append(Restart(mono))
                case "suspend":
                    events.append(Suspend(mono))
                case "resume":
                    events.append(Resume(mono))
                case "limits" if payload is not None:
                    events.append(LimitsChanged(mono, _LIMITS.decode(payload)))
                case "rules" if payload is not None:
                    events.append(RulesLoaded(mono, tuple(_RULES.decode(payload))))
                case "settings":
                    events.append(EngineSettings(mono, number))
                case _:
                    log.warning("store: skipping an unreadable input row (frame %d)", frame)
        if tick is not None:
            events.append(Tick(tick[1], tick[2]))
        return events

    def close(self) -> None:
        self.flush()
        self._db.close()
