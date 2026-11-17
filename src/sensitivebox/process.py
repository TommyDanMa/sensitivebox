"""Bounded child processes with captured, redacted output and no shell."""

import subprocess
from collections.abc import Sequence

from sensitivebox.redact import redact


def run(args: Sequence[str]) -> str:
    try:
        result = subprocess.run(  # noqa: S603 - argument vectors only, no shell
            list(args),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise OSError(redact(f"{args[0]} timed out after 10 s")) from None
    if result.returncode:
        raise OSError(
            redact(result.stderr or result.stdout or f"{args[0]} failed ({result.returncode})")
        )
    return redact(result.stdout)
