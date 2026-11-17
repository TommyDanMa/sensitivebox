"""Host facts from selected files only; absent fixture inputs stay null."""

from pathlib import Path

from sensitivebox import __version__
from sensitivebox.api.model import HostView


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except (OSError, UnicodeError):
        return None


def host_view(proc_root: Path, os_release: Path) -> HostView:
    release = None
    for line in (_read(os_release) or "").splitlines():
        key, sep, value = line.partition("=")
        if sep and key == "PRETTY_NAME":
            release = value.strip().strip('"').strip("'") or None
    return HostView(
        _read(proc_root / "sys/kernel/hostname"),
        release,
        _read(proc_root / "sys/kernel/osrelease"),
        __version__,
    )
