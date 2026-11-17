"""Read single sysfs attribute files, turning every kind of read failure into a reason.

hwmon attributes fail in many ways: ``-ENODATA`` when the firmware has no value, ``-EIO`` when a
bus transaction fails, ``-EACCES`` for root-only attributes, or a file that vanishes when a device
is unplugged mid-scan. None of these may crash the daemon, so all of them mean "unavailable", and
the reason is kept so ``sensitivebox doctor`` can say why.
"""

import errno
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

type Opener = Callable[[Path], str]

_MAX_SHOWN = 20  # characters of a non-numeric value quoted in a reason


def default_opener(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class RawRead:
    """An attribute read as an integer: exactly one of ``value`` and ``reason`` is set."""

    value: int | None
    reason: str | None = None


def _error_name(exc: OSError) -> str:
    if exc.errno is not None and exc.errno in errno.errorcode:
        return errno.errorcode[exc.errno]  # e.g. "ENODATA", "EACCES", "ENOENT"
    return type(exc).__name__


def _read_text(path: Path, opener: Opener) -> tuple[str | None, str | None]:
    try:
        return opener(path).strip(), None
    except OSError as exc:
        return None, _error_name(exc)


def read_attr(path: Path, opener: Opener = default_opener) -> str | None:
    """Return the stripped contents of an attribute file, or ``None`` if it can't be read."""
    return _read_text(path, opener)[0]


def read_raw(path: Path, opener: Opener = default_opener) -> RawRead:
    """Read an attribute as an integer, keeping the reason when that isn't possible."""
    text, error = _read_text(path, opener)
    if text is None:
        return RawRead(None, error)
    if not text:
        return RawRead(None, "empty")
    try:
        return RawRead(int(text))
    except ValueError:
        shown = text if len(text) <= _MAX_SHOWN else text[:_MAX_SHOWN] + "…"
        return RawRead(None, f"not a number: {shown!r}")


def read_int(path: Path, opener: Opener = default_opener) -> int | None:
    """Return an attribute as an integer, or ``None`` if unreadable, empty or non-numeric."""
    return read_raw(path, opener).value
