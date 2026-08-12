"""A tiny `/metrics` and `/health` listener for the worker.

Stdlib only, and here the "no framework for two endpoints" argument genuinely
does hold: this serves two fixed strings on a thread, and a worker's image
should not carry a web framework so that Prometheus can ask it a question.

Nothing scrapes these yet. They exist so that when something does, no rework is
needed — the numbers are already being computed, and `HostLimiter` has been
throwing away its backoff and concurrency decisions since the beginning.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from mb_ceramics_catalogue.observability import logging as obs
from mb_ceramics_catalogue.observability import metrics

LOGGER = obs.get_logger("catalogue.metrics")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    #: Set by `serve`, so the health endpoint can report what the worker thinks.
    status: Any = None

    def log_message(self, fmt: str, *args: Any) -> None:
        # The default writes to stderr in Apache format, which would appear in
        # the middle of the worker's structured JSON.
        LOGGER.debug("metrics.request", request=fmt % args)

    def do_GET(self) -> None:
        if self.path.rstrip("/") in ("/metrics", ""):
            self._respond(200, metrics.render(), "text/plain; version=0.0.4")
        elif self.path.rstrip("/") == "/health":
            state = _Handler.status() if callable(_Handler.status) else {"status": "ok"}
            healthy = state.get("status") != "stopped"
            self._respond(
                200 if healthy else 503,
                _json(state),
                "application/json",
            )
        else:
            self._respond(404, '{"title":"Not Found","status":404}', "application/problem+json")

    def _respond(self, status: int, body: str, content_type: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, default=str)


def serve(port: int, status: Any = None) -> ThreadingHTTPServer | None:
    """Start the listener on a daemon thread, and never let it fail the worker.

    A worker that cannot bind its metrics port must still crawl. Losing the
    numbers is a nuisance; losing the run is not.
    """
    if port <= 0:
        return None
    _Handler.status = staticmethod(status) if status else None
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    except OSError as error:
        LOGGER.warning("metrics.unavailable", port=port, error=str(error))
        return None

    thread = threading.Thread(target=server.serve_forever, name="metrics", daemon=True)
    thread.start()
    LOGGER.info("metrics.listening", port=port)
    return server
