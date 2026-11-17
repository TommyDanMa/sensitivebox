"""Local ASGI routes; all handlers execute on the daemon thread, including SQLite access."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import msgspec
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import Message, Receive, Scope, Send

from sensitivebox.alerts.model import parse_duration
from sensitivebox.api.data import history, incident, incidents
from sensitivebox.api.host import host_view
from sensitivebox.api.model import AckBody, ErrorView, MuteBody
from sensitivebox.api.security import ApiSecurity
from sensitivebox.api.state import ApiState

MAX_BODY = 4096


class BuiltFileResponse(FileResponse):
    """Keep successful assets unchanged; route library-generated errors through redaction."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def checked(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] >= 400:
                content_range = Headers(raw=message["headers"]).get("content-range")
                headers = {"Content-Range": content_range} if content_range else None
                raise HTTPException(message["status"], "invalid static file range", headers=headers)
            await send(message)

        await super().__call__(scope, receive, checked)


class LiveResponse(StreamingResponse):
    """Cancel even a blocked socket send when a subscriber's queue overflows."""

    def __init__(self, state: ApiState) -> None:
        self.state = state
        self.sub = state.subscribe()
        super().__init__(
            state.stream(self.sub),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    async def stream_response(self, send: Send) -> None:
        sender = asyncio.create_task(super().stream_response(send))
        closed = asyncio.create_task(self.sub.closed.wait())
        try:
            done, _ = await asyncio.wait((sender, closed), return_when=asyncio.FIRST_COMPLETED)
            if sender in done:
                await sender
            else:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
                # If even the final frame is blocked, propagate a generic timeout so the
                # ASGI server closes the transport instead of retaining a slow client.
                async with asyncio.timeout(0.1):
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            sender.cancel()
            closed.cancel()
            await asyncio.gather(sender, closed, return_exceptions=True)
            self.state.unsubscribe(self.sub)


def create_app(
    state: ApiState,
    *,
    proc_root: Path,
    os_release: Path,
    static_root: Path | None = None,
) -> Starlette:
    static = (
        static_root if static_root is not None else Path(__file__).parent / "static"
    ).resolve()
    facts = host_view(proc_root, os_release)

    def reply(value: object, code: int = 200) -> Response:
        return Response(state.encode(value), status_code=code, media_type="application/json")

    async def error(request: Request, exc: Exception) -> Response:
        if isinstance(exc, HTTPException):
            response = reply(ErrorView(str(exc.detail)), exc.status_code)
            response.headers.update(exc.headers or {})
            return response
        return reply(ErrorView("API unavailable; monitoring continues"), 500)

    async def sensors(request: Request) -> Response:
        view = state.snapshot()
        if view is None:
            raise HTTPException(503, "waiting for the first poll")
        return reply(view)

    async def host(request: Request) -> Response:
        return reply(facts)

    async def read_history(request: Request) -> Response:
        identity = request.query_params.get("id")
        period = request.query_params.get("range")
        if not identity or period not in {"1h", "6h", "24h"}:
            raise HTTPException(400, "history requires id and range=1h|6h|24h")
        state.store.flush()
        return reply(history(state.store.mutes.db, identity, period, state.wall(), state.interval))

    async def alerts(request: Request) -> Response:
        try:
            limit = int(request.query_params.get("limit", "100"))
        except ValueError:
            raise HTTPException(400, "limit must be 1-10000") from None
        if not 1 <= limit <= 10000:
            raise HTTPException(400, "limit must be 1-10000")
        return reply(
            incidents(
                state.store.mutes.db,
                state.wall(),
                limit,
                boot=state.boot,
                monotonic_now=state.clock(),
            )
        )

    async def mutate(request: Request) -> Response:
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise HTTPException(415, "use application/json")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                raise HTTPException(413, "JSON body too large")
        muting = request.path_params["action"] == "mute"
        try:
            payload = (
                msgspec.json.decode(body, type=MuteBody)
                if muting
                else msgspec.json.decode(body, type=AckBody)
            )
            expiry = None
            if isinstance(payload, MuteBody) and payload.duration != "until-reboot":
                seconds = parse_duration(payload.duration)
                if seconds <= 0:
                    raise ValueError("duration must be positive")
                expiry = state.clock() + seconds
        except (msgspec.DecodeError, ValueError):
            raise HTTPException(400, "invalid JSON or mutation fields") from None
        db = state.store.mutes.db
        current = incident(db, str(request.path_params["id"]))
        if current is None:
            raise HTTPException(404, "unknown incident")
        if current.closed is not None:
            raise HTTPException(409, "incident already closed")
        with db:
            if isinstance(payload, MuteBody):
                # Same control plane and boot/monotonic scope as the CLI, no direct routing.
                state.store.mutes.set(current.rule_id, state.boot, expiry, payload.renotify)
                state.store.events.fact(current.id, "muted")
            else:
                state.store.events.fact(current.id, "acknowledged")
        updated = incident(db, current.id)
        if updated is None:
            raise HTTPException(404, "unknown incident")
        if muting or not current.acknowledged:
            state.changed(updated, "muted" if muting else "acknowledged")
        return reply(updated)

    async def ack(request: Request) -> Response:
        request.path_params["action"] = "ack"
        return await mutate(request)

    async def mute(request: Request) -> Response:
        request.path_params["action"] = "mute"
        return await mutate(request)

    async def stream(request: Request) -> Response:
        return LiveResponse(state)

    async def fallback(request: Request) -> Response:
        route = request.path_params.get("path", "")
        if route == "api" or route.startswith("api/"):
            raise HTTPException(404, "API route not found")
        index = static / "index.html"
        if not index.resolve().is_relative_to(static) or not index.is_file():
            raise HTTPException(503, "The UI has not been built. Run cd ui && pnpm build.")
        target = (static / route).resolve()
        if not target.is_relative_to(static):
            raise HTTPException(404, "file not found")
        if target.is_file():
            return BuiltFileResponse(target)
        if Path(route).suffix:
            raise HTTPException(404, "file not found")
        return BuiltFileResponse(index)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            state.close()

    return Starlette(
        routes=[
            Route("/api/sensors", sensors),
            Route("/api/host", host),
            Route("/api/history", read_history),
            Route("/api/alerts", alerts),
            Route("/api/alerts/{id}/ack", ack, methods=["POST"]),
            Route("/api/alerts/{id}/mute", mute, methods=["POST"]),
            Route("/api/stream", stream),
            Route("/{path:path}", fallback),
        ],
        middleware=[Middleware(ApiSecurity, state=state)],
        exception_handlers={HTTPException: error, Exception: error},
        lifespan=lifespan,
    )
