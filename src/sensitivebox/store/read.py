"""Read-only transactions for operations history and exact replay."""

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from sensitivebox.alerts.events import Checkpoint, InputEvent
from sensitivebox.store import schema
from sensitivebox.store.db import Store


@contextmanager
def reader(path: Path) -> Generator[sqlite3.Connection | None]:
    if not path.exists():
        yield None
        return
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        if db.execute("PRAGMA user_version").fetchone()[0] != schema.VERSION:
            raise sqlite3.DatabaseError("history needs a compatible writer; start sbox watch")
        yield db
    finally:
        db.close()


def inputs(path: Path) -> list[InputEvent]:
    with reader(path) as db:
        if db is None:
            return []
        rows = db.execute(schema.SELECT_INPUTS).fetchall()
        checkpoint = db.execute("SELECT frame,mono,payload FROM replay_checkpoint").fetchone()
        if checkpoint:
            return [
                Checkpoint(checkpoint[1], checkpoint[2]),
                *Store.decode_inputs([r for r in rows if r[0] >= checkpoint[0]]),
            ]
        return Store.decode_inputs(rows)
