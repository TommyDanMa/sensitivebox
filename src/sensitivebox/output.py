"""One redacted boundary for terminal documents, events and errors."""

import logging
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, cast

import msgspec

from sensitivebox.redact import redact


class Document(msgspec.Struct, frozen=True):
    kind: str
    data: Any
    version: int = 1


def _private(value: Any, redactor: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return redactor(value)
    if isinstance(value, dict):
        return {redactor(k): _private(v, redactor) for k, v in cast(dict[str, Any], value).items()}
    if isinstance(value, list):
        return [_private(v, redactor) for v in cast(list[Any], value)]
    return value


def encode(value: object, *, redactor: Callable[[str], str] = redact) -> str:
    """Redact fields before encoding, preserving JSON escapes and numeric types."""
    return msgspec.json.encode(_private(msgspec.to_builtins(value), redactor)).decode()


@dataclass(frozen=True, slots=True)
class Output:
    json: bool = False

    def emit(self, kind: str, data: object, text: str, *, error: bool = False) -> None:
        line = encode(Document(kind, data)) if self.json else redact(text)
        print(line, file=sys.stderr if error else sys.stdout, flush=True)

    def line(self, text: str) -> None:
        self.emit("message", text, text)

    def error(self, text: str) -> None:
        self.emit("error", text, f"sbox: {text}", error=True)


# CLI-owned logging uses the same boundary, including exception tracebacks.


class _Handler(logging.Handler):
    def __init__(self, output: Output) -> None:
        super().__init__()
        self.output = output

    def emit(self, record: logging.LogRecord) -> None:
        text = self.format(record)
        self.output.emit("log", text, text, error=True)


@contextmanager
def logging_output(output: Output) -> Generator[None]:
    logger = logging.getLogger("sensitivebox")
    handlers, propagate = logger.handlers[:], logger.propagate
    handler = _Handler(output)
    logger.handlers = [handler]
    logger.propagate = False
    try:
        yield
    finally:
        logger.handlers = handlers
        logger.propagate = propagate
        handler.close()
