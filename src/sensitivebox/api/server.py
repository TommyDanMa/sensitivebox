"""Embedded uvicorn listener: pre-bind IPv4 loopback, leave daemon signal handling alone."""

import asyncio
import errno
import logging
import socket
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import asynccontextmanager, contextmanager
from typing import Protocol

import uvicorn
from starlette.types import ASGIApp

from sensitivebox.redact import redact


class Server(Protocol):
    should_exit: bool

    async def serve(self, sockets: list[socket.socket] | None = None) -> None: ...


class EmbeddedServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Generator[None]:
        """The CLI owns SIGINT/SIGTERM; never replace or re-raise those handlers."""
        yield


class RedactedLog(logging.Handler):
    def __init__(self, out: Callable[[str], None]) -> None:
        super().__init__()
        self.out = out

    def emit(self, record: logging.LogRecord) -> None:
        self.out(redact(self.format(record)))


@asynccontextmanager
async def serve_api(
    app: ASGIApp,
    out: Callable[[str], None],
    *,
    socket_factory: Callable[[], socket.socket] = socket.socket,
    server_factory: Callable[[uvicorn.Config], Server] = EmbeddedServer,
) -> AsyncGenerator[None]:
    listener = socket_factory()
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 7878))
        listener.listen(128)
        listener.setblocking(False)
    except OSError as exc:
        listener.close()
        if exc.errno != errno.EADDRINUSE:
            raise
        out(redact("API unavailable: 127.0.0.1:7878 is already in use; monitoring continues"))
        yield
        return

    logger = logging.getLogger("uvicorn.error")
    previous, propagate = logger.handlers[:], logger.propagate
    handler = RedactedLog(out)
    logger.handlers, logger.propagate = [handler], False
    task: asyncio.Task[None] | None = None
    try:
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=7878,
            loop="asyncio",
            http="h11",
            ws="none",
            lifespan="on",
            log_config=None,
            access_log=False,
            proxy_headers=False,
            server_header=False,
            timeout_graceful_shutdown=1,
        )
        server = server_factory(config)
        task = asyncio.create_task(server.serve(sockets=[listener]), name="sbox-api")

        def finished(done: asyncio.Task[None]) -> None:
            if not done.cancelled() and done.exception() is not None:
                out(redact("API unavailable: server stopped; monitoring continues"))

        task.add_done_callback(finished)
        try:
            yield
        finally:
            server.should_exit = True
            # A stuck client must never keep monitoring's shutdown alive indefinitely.
            try:
                async with asyncio.timeout(2):
                    await asyncio.shield(task)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except Exception:  # noqa: S110 - already reported through the redacted callback
                pass  # already reported through the redacted callback
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        listener.close()
        logger.handlers, logger.propagate = previous, propagate
        handler.close()
