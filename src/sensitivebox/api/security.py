"""Loopback Host, same-origin Origin, and daemon-session CSRF for API mutations."""

import ipaddress
import secrets
from urllib.parse import urlsplit

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from sensitivebox.api.model import ErrorView
from sensitivebox.api.state import ApiState


def loopback(host: str) -> bool:
    try:
        parsed = urlsplit("//" + host)
        if parsed.username is not None or parsed.password is not None or parsed.path:
            return False
        if parsed.query or parsed.fragment or not parsed.hostname:
            return False
        _ = parsed.port  # reject malformed ports
        return parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


class ApiSecurity:
    def __init__(self, app: ASGIApp, *, state: ApiState) -> None:
        self.app, self.state = app, state
        self.token = secrets.token_urlsafe(32)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        started = False
        api = scope["path"] == "/api" or scope["path"].startswith("/api/")
        if api and request.method == "POST" and not self.allowed(request):
            response = Response(
                self.state.encode(ErrorView("loopback origin and CSRF required")),
                status_code=403,
                media_type="application/json",
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return

        async def guarded(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            if api and message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["cache-control"] = "no-store"
                headers["x-content-type-options"] = "nosniff"
                if request.method == "GET":
                    cookie = Response()
                    cookie.set_cookie("sbox_csrf", self.token, samesite="strict", path="/")
                    headers.append("set-cookie", cookie.headers["set-cookie"])
            await send(message)

        try:
            await self.app(scope, receive, guarded)
        except Exception:
            if started:
                raise
            response = Response(
                self.state.encode(ErrorView("API unavailable; monitoring continues")),
                status_code=500,
                media_type="application/json",
            )
            await response(scope, receive, guarded)

    def allowed(self, request: Request) -> bool:
        headers = request.headers
        if any(len(headers.getlist(name)) != 1 for name in ("host", "origin", "x-csrf-token")):
            return False
        host = headers["host"]
        return (
            loopback(host)
            and headers["origin"] == f"{request.scope['scheme']}://{host}"
            and secrets.compare_digest(
                request.cookies.get("sbox_csrf", "").encode(), self.token.encode()
            )
            and secrets.compare_digest(headers["x-csrf-token"].encode(), self.token.encode())
        )
