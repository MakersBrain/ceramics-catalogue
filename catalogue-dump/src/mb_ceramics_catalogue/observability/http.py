"""Bounded request telemetry shared by the two Python HTTP services."""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any
from uuid import uuid4

from mb_ceramics_catalogue.observability import logging as obs
from mb_ceramics_catalogue.observability import metrics

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
QUIET_PATHS = frozenset({"/health", "/metrics"})


def request_id(value: str | None) -> str:
    """Accept a bounded opaque correlation id or create one locally."""
    return value if value and REQUEST_ID_PATTERN.fullmatch(value) else str(uuid4())


class RequestTelemetry:
    """Raw ASGI access logging, request IDs, and the small API RED metric set."""

    def __init__(self, app: ASGIApp, service: str, routes: list[Any] | None = None) -> None:
        self.app = app
        self.service = service
        self.routes = routes or []
        self.log = obs.get_logger(f"catalogue.{service}.http")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        identifier = request_id(_header(scope, b"x-request-id"))
        scope["request_id"] = identifier
        started = time.monotonic()
        status = 500
        response_started = False
        streaming = scope.get("path") == "/v1/events"
        metrics.http_request_in_flight(self.service, 1)

        async def send_with_context(message: Message) -> None:
            nonlocal response_started, status
            if message["type"] == "http.response.start":
                response_started = True
                status = int(message["status"])
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", identifier.encode("ascii")))
                message["headers"] = headers
            await send(message)

        try:
            with obs.bound(request_id=identifier):
                try:
                    await self.app(scope, receive, send_with_context)
                except Exception:
                    # Starlette's ServerErrorMiddleware is outside user
                    # middleware, so its fallback response would bypass our
                    # header wrapper and its traceback would be logged after
                    # this request context was reset. Send the same safe 500
                    # here, then re-raise so the server still records the fault.
                    self.log.exception(
                        "http.unhandled",
                        service=self.service,
                        method=scope["method"],
                        route=_route_template(scope, self.routes),
                        request_id=identifier,
                    )
                    if not response_started:
                        body = b"Internal Server Error"
                        try:
                            await send_with_context(
                                {
                                    "type": "http.response.start",
                                    "status": 500,
                                    "headers": [
                                        (b"content-type", b"text/plain; charset=utf-8"),
                                        (b"content-length", str(len(body)).encode("ascii")),
                                    ],
                                }
                            )
                            await send_with_context(
                                {"type": "http.response.body", "body": body}
                            )
                        except Exception:  # noqa: BLE001 - the client may already be gone
                            pass
                    raise
        finally:
            elapsed = time.monotonic() - started
            metrics.http_request_in_flight(self.service, -1)
            route = _route_template(scope, self.routes)
            metrics.http_request(
                self.service,
                str(scope["method"]),
                route,
                status,
                None if streaming else elapsed,
            )
            if scope.get("path") not in QUIET_PATHS or status >= 400:
                self.log.info(
                    "http.request",
                    service=self.service,
                    method=scope["method"],
                    route=route,
                    status=status,
                    duration_ms=round(elapsed * 1000, 3),
                    request_id=identifier,
                    operator=scope.get("operator"),
                    streaming=streaming or None,
                )


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            try:
                return bytes(value).decode("ascii")
            except UnicodeDecodeError:
                return None
    return None


def _route_template(scope: Scope, configured: list[Any] | None = None) -> str:
    endpoint = scope.get("endpoint")
    router = scope.get("router")
    for route in getattr(router, "routes", ()):
        if getattr(route, "endpoint", None) is endpoint:
            return str(getattr(route, "path", "unmatched"))
    # An outer authentication middleware may reject before Starlette's router
    # enriches the scope. The app supplies its bounded route table so those
    # failures still use a template rather than a concrete path.
    path = str(scope.get("path", ""))
    for route in configured or ():
        pattern = getattr(route, "path_regex", None)
        if pattern is not None and pattern.match(path):
            return str(getattr(route, "path", "unmatched"))
    return "unmatched"
