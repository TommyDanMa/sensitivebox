"""Read-only version observations and incremental, bounded apt history tracking."""

import gzip
import hashlib
from pathlib import Path

from sensitivebox.store.changes import Changes

MAX_HISTORY = 4 * 1024 * 1024
MAX_FILES = 16


def _read(path: Path) -> str:
    try:
        return path.read_text()[:4096].strip()
    except OSError:
        return ""


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ChangeTracker:
    def __init__(self, store: Changes, sysfs_root: Path, proc_root: Path, apt_root: Path) -> None:
        self.store, self.sysfs_root, self.proc_root, self.apt_root = (
            store,
            sysfs_root,
            proc_root,
            apt_root,
        )

    def scan(self, version: str) -> None:
        self.store.observe("kernel", _read(self.proc_root / "sys/kernel/osrelease"))
        self.store.observe("bios", _read(self.sysfs_root / "class/dmi/id/bios_version"))
        self.store.observe("sensitivebox", version)
        self._apt()

    def _apt(self) -> None:
        db = self.store.db
        initialized = (
            db.execute("SELECT 1 FROM change_baselines WHERE kind='apt-initialized'").fetchone()
            is not None
        )
        try:
            files = sorted(self.apt_root.glob("history.log*"), key=lambda p: p.stat().st_mtime)[
                -MAX_FILES:
            ]
        except OSError:
            return
        current_read = False
        with db:
            for path in files:
                try:
                    stat = path.stat()
                    identity = f"{stat.st_dev}:{stat.st_ino}"
                    with (
                        gzip.GzipFile(path, "rb") if path.suffix == ".gz" else path.open("rb")
                    ) as source:
                        size = source.seek(0, 2)
                        source.seek(0)
                        head = source.read(4096)
                        row = db.execute(
                            "SELECT offset,digest,pending FROM apt_cursors WHERE identity=?",
                            (identity,),
                        ).fetchone()
                        if row is None:
                            # Compression changes inodes. The consumed head and offset survive it.
                            row = next(
                                (
                                    r
                                    for r in db.execute(
                                        "SELECT offset,digest,pending FROM apt_cursors"
                                        " ORDER BY offset DESC"
                                    )
                                    if 0 < r[0] <= size and _digest(head[: min(r[0], 4096)]) == r[1]
                                ),
                                None,
                            )
                        if not initialized:
                            offset, pending = size, ""
                        elif (
                            row and row[0] <= size and _digest(head[: min(row[0], 4096)]) == row[1]
                        ):
                            offset, pending = row[0], row[2]
                        else:
                            offset, pending = 0, ""
                        source.seek(offset)
                        data = source.read(MAX_HISTORY)  # bounded work; the next scan drains more
                except (OSError, EOFError):
                    continue
                current_read |= path.name == "history.log"
                pending += data.decode("utf-8", errors="replace")
                while "\nEnd-Date:" in pending:
                    end = pending.find("\n", pending.index("\nEnd-Date:") + 1)
                    if end < 0:
                        break
                    block, pending = pending[: end + 1], pending[end + 1 :]
                    upgrade = next(
                        (line for line in block.splitlines() if line.startswith("Upgrade:")), ""
                    )
                    if upgrade:
                        self.store.marker(
                            "apt",
                            upgrade.removeprefix("Upgrade:").strip(),
                            _digest(block.strip().encode()),
                        )
                consumed = offset + len(data)
                db.execute(
                    "INSERT OR REPLACE INTO apt_cursors VALUES(?,?,?,?,?)",
                    (
                        identity,
                        consumed,
                        _digest(head[: min(consumed, 4096)]),
                        pending[-MAX_HISTORY:],
                        self.store.clock(),
                    ),
                )
            # Permission gaps start at the first readable current EOF, never import old history.
            if current_read:
                db.execute("INSERT OR IGNORE INTO change_baselines VALUES('apt-initialized','1')")
