"""SQLite schema, retention and the SQL statements. All statements are static text with bound
parameters: nothing is ever formatted into SQL.

Retention (docs/BIBLE.md, "Storage" and "Alerting"):

* ``inputs_raw``: a 1 h ring of **input frames**, everything the alert engine saw, in order: each
  poll's signal values or MISSING reasons (guard signals included), plus lifecycle events (rules
  loaded or edited, chip limits changed, suspend, resume). Replaying it through the engine gives
  exactly what the live daemon did (review ADV-005). One poll is a ``tick`` row followed by its
  ``value`` rows, all sharing a ``frame`` number; a lifecycle event is one row whose ``payload``
  is msgspec JSON. Pruning keeps the newest ``rules``, ``limits`` and ``settings`` rows from before
  the cutoff,
  so a replay of the ring knows the configuration in force at its start.
* ``metrics_raw``: values that are stored but never alerted on (network rates), 1 h.
* ``evaluations_raw``: every rule evaluation, derived from the frames and kept for display, 1 h.
* ``rollup_1m``: one-minute count/sum/min/max, kept 7 days.
* ``rollup_15m``: fifteen-minute count/sum/min/max, kept **400 days**. Insights depends on these;
  never drop or shorten them (``tests/store/test_retention.py`` pins the number).

Rollups keep ``n`` and ``total`` (not an average) so a bucket written in two pieces, by a restart
or two flushes, merges exactly.
"""

from sensitivebox.store.changes import CREATE as CHANGES_CREATE
from sensitivebox.store.events import CREATE as OPERATIONS_CREATE
from sensitivebox.store.mutes import CREATE as MUTES_CREATE

RAW_RETENTION_S = 3600
ROLLUP_1M_WIDTH_S = 60
ROLLUP_1M_RETENTION_S = 7 * 86_400
ROLLUP_15M_WIDTH_S = 15 * 60
ROLLUP_15M_RETENTION_S = 400 * 86_400

VERSION = 10  # bump with any schema change
GET_VERSION = "PRAGMA user_version"
SET_VERSION = f"PRAGMA user_version = {VERSION}"

# Version 1 kept signal values in samples_raw, without the engine's clock or lifecycle events, so
# it couldn't be replayed. It was only a 1 h ring: drop it. The rollups carry over unchanged.
UPGRADE_FROM_1 = ("DROP TABLE IF EXISTS samples_raw",)

PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA foreign_keys = ON",
)

CREATE = (
    *OPERATIONS_CREATE,
    *MUTES_CREATE,
    *CHANGES_CREATE,
    """CREATE TABLE IF NOT EXISTS replay_checkpoint (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), frame INTEGER NOT NULL,
        mono REAL NOT NULL, payload TEXT NOT NULL)""",
    """
    CREATE TABLE IF NOT EXISTS signals (
        id   INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS inputs_raw (
        frame     INTEGER NOT NULL,
        ts        REAL NOT NULL,
        mono      REAL NOT NULL,
        kind      TEXT NOT NULL,
        signal_id INTEGER REFERENCES signals(id),
        value     REAL,
        text      TEXT,
        missing   TEXT,
        payload   TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS inputs_raw_ts ON inputs_raw (ts)",
    "CREATE INDEX IF NOT EXISTS inputs_raw_frame ON inputs_raw (frame)",
    "CREATE INDEX IF NOT EXISTS inputs_raw_signal ON inputs_raw (signal_id, ts)",
    """
    CREATE TABLE IF NOT EXISTS metrics_raw (
        ts        REAL NOT NULL,
        signal_id INTEGER NOT NULL REFERENCES signals(id),
        value     REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS metrics_raw_ts ON metrics_raw (ts)",
    """
    CREATE TABLE IF NOT EXISTS evaluations_raw (
        ts        REAL NOT NULL,
        rule_id   TEXT NOT NULL,
        signal_id INTEGER NOT NULL REFERENCES signals(id),
        value     REAL,
        text      TEXT,
        missing   TEXT,
        state     TEXT NOT NULL,
        reason    TEXT NOT NULL,
        threshold REAL,
        alert_severity TEXT,
        no_data INTEGER NOT NULL DEFAULT 0,
        metadata_known INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS evaluations_raw_ts ON evaluations_raw (ts)",
    "CREATE INDEX IF NOT EXISTS evaluations_raw_rule ON evaluations_raw (rule_id, ts)",
    """
    CREATE TABLE IF NOT EXISTS rollup_1m (
        bucket    INTEGER NOT NULL,
        signal_id INTEGER NOT NULL REFERENCES signals(id),
        n         INTEGER NOT NULL,
        total     REAL NOT NULL,
        lo        REAL NOT NULL,
        hi        REAL NOT NULL,
        PRIMARY KEY (bucket, signal_id)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS rollup_15m (
        bucket    INTEGER NOT NULL,
        signal_id INTEGER NOT NULL REFERENCES signals(id),
        n         INTEGER NOT NULL,
        total     REAL NOT NULL,
        lo        REAL NOT NULL,
        hi        REAL NOT NULL,
        PRIMARY KEY (bucket, signal_id)
    ) WITHOUT ROWID
    """,
)

INSERT_SIGNAL = "INSERT OR IGNORE INTO signals (name) VALUES (?)"
SELECT_SIGNAL = "SELECT id FROM signals WHERE name = ?"

INSERT_INPUT = (
    "INSERT INTO inputs_raw (frame, ts, mono, kind, signal_id, value, text, missing, payload)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
LAST_FRAME = "SELECT coalesce(max(frame), 0) FROM inputs_raw"
SELECT_INPUTS = """
    SELECT i.frame, i.mono, i.kind, s.name, i.value, i.text, i.missing, i.payload
    FROM inputs_raw AS i LEFT JOIN signals AS s ON s.id = i.signal_id
    ORDER BY i.frame, i.rowid
"""
INSERT_METRIC = "INSERT INTO metrics_raw (ts, signal_id, value) VALUES (?, ?, ?)"
INSERT_EVALUATION = (
    "INSERT INTO evaluations_raw"
    " (ts, rule_id, signal_id, value, text, missing, state, reason, threshold,"
    " alert_severity, no_data, metadata_known)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)"
)

UPSERT_1M = """
    INSERT INTO rollup_1m (bucket, signal_id, n, total, lo, hi) VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT (bucket, signal_id) DO UPDATE SET
        n = n + excluded.n,
        total = total + excluded.total,
        lo = min(lo, excluded.lo),
        hi = max(hi, excluded.hi)
"""
UPSERT_15M = """
    INSERT INTO rollup_15m (bucket, signal_id, n, total, lo, hi) VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT (bucket, signal_id) DO UPDATE SET
        n = n + excluded.n,
        total = total + excluded.total,
        lo = min(lo, excluded.lo),
        hi = max(hi, excluded.hi)
"""

# Raw rows older than the ring go, except the newest rules and limits events before the cutoff.
# A rollup bucket goes only once the *whole* bucket is older than its retention
# (bucket start + width <= cutoff), so retention is never cut short. PRUNE_INPUTS takes the
# cutoff four times.
PRUNE_INPUTS = """
    DELETE FROM inputs_raw WHERE ts < ? AND frame NOT IN (
        SELECT coalesce(max(frame), -1) FROM inputs_raw WHERE kind = 'rules' AND ts < ?
        UNION ALL
        SELECT coalesce(max(frame), -1) FROM inputs_raw WHERE kind = 'limits' AND ts < ?
        UNION ALL
        SELECT coalesce(max(frame), -1) FROM inputs_raw WHERE kind = 'settings' AND ts < ?
    )
"""
PRUNE_CHECKPOINT_INPUTS = PRUNE_INPUTS.replace(
    "WHERE ts < ? AND frame NOT IN", "WHERE ts < ? AND frame < ? AND frame NOT IN"
)
PRUNE_METRICS = "DELETE FROM metrics_raw WHERE ts < ?"
PRUNE_EVALUATIONS = "DELETE FROM evaluations_raw WHERE ts < ?"
PRUNE_1M = "DELETE FROM rollup_1m WHERE bucket + ? <= ?"
PRUNE_15M = "DELETE FROM rollup_15m WHERE bucket + ? <= ?"

# Only the writer migrates: status readers never execute these statements.
UPGRADE_FROM_2 = (
    "ALTER TABLE evaluations_raw ADD COLUMN alert_severity TEXT",
    "ALTER TABLE evaluations_raw ADD COLUMN no_data INTEGER NOT NULL DEFAULT 0",
)

UPGRADE_FROM_3 = (
    "ALTER TABLE evaluations_raw ADD COLUMN metadata_known INTEGER NOT NULL DEFAULT 0",
)
