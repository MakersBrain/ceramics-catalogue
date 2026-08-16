"""Logging, configured once, from an entry point and nowhere else.

Two things in the old code performed surgery on the root logger.
`ProgressReporter.open()` did `logging.getLogger().handlers = [handler]` and
restored from an instance attribute; `tui.Dashboard.capture_logging()` did the
same with a `RichHandler`. Both also reached in and changed the `httpx` logger's
level. It worked, and it was the display layer reconfiguring global logging as a
side effect of being constructed.

That has to go regardless of style, because a worker must keep emitting
structured JSON to stdout the whole time it is running — its logs are how you
debug it — and a display that swaps the handler list would silently take that
away. So:

* **Displays add a handler.** `attach()` appends and returns a token; `detach()`
  removes exactly that one. Nothing ever assigns to `.handlers`.
* **The console handler is quietened through a documented switch**, `quieten()`,
  rather than by having the list swapped underneath it.
* **Context comes from contextvars.** `bind(run_id=…, job_id=…, source=…)` puts
  the identifiers on every line emitted inside that task, which fits the
  existing `CURRENT_SOURCE` pattern exactly. `LOGGER.info("started source=%s",
  source)` was hand-formatting what a contextvar already knew.
* **Event names, not sentences.** `log.info("job.started", scraper=…)` — one
  vocabulary across stdout, `catalogue.job_events` and the SSE stream (§9).
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog
from structlog.typing import EventDict

#: The handler the process writes its own logs through. Kept so `quieten()` can
#: find it without going through the root logger's list, which anything may add
#: to.
_console: logging.Handler | None = None
_configured = False
_secrets: set[str] = set()
_userinfo = re.compile(r"(?P<scheme>https?://)[^/@\s]+@", re.IGNORECASE)


def register_secrets(values: set[str]) -> None:
    """Add runtime credentials to the process-wide structured-log scrubber."""
    _secrets.update(value for value in values if value)


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        cleaned = _userinfo.sub(r"\g<scheme>[REDACTED]@", value)
        for secret in _secrets:
            cleaned = cleaned.replace(secret, "[REDACTED]")
        return cleaned
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(item) for item in value]
    return value


def _redact_event(_: Any, __: str, event: EventDict) -> EventDict:
    return {key: _scrub(value) for key, value in event.items()}


def scrub(value: Any) -> Any:
    """Scrub a value before it leaves the configured logging pipeline.

    Database log sinks do not pass through the console renderer, so they use
    this public boundary rather than importing the private recursive helper.
    """
    return _scrub(value)


def _add_trace_context(_: Any, __: str, event: EventDict) -> EventDict:
    # Tracing is an optional extra while logging is not. Keep this import lazy so
    # configuring ordinary structured logs never makes OpenTelemetry mandatory.
    from . import tracing

    if identifier := tracing.trace_id():
        event.setdefault("trace_id", identifier)
    return event


def configure(level: str = "INFO", *, json: bool | None = None, stream: Any = None) -> None:
    """Set up structlog and the stdlib root logger. Idempotent.

    `json` defaults to "whichever suits the destination": a terminal gets the
    readable renderer, a pipe or a container log gets one JSON object per line.
    Passing it explicitly is for the cases where that guess is wrong — a
    developer wanting to see exactly what a shipper would.
    """
    global _console, _configured

    destination = stream if stream is not None else sys.stderr
    as_json = (not destination.isatty()) if json is None else json

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_trace_context,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if as_json
        else structlog.dev.ConsoleRenderer(colors=destination.isatty())
    )

    structlog.configure(
        processors=[
            *shared,
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Records that came through the stdlib — httpx, asyncio, and the scrapers,
    # which still use `LOGGER.warning("host=%s ...", host)` and are not being
    # rewritten — are rendered by the same formatter, so one run produces one
    # kind of line rather than two.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            _redact_event,
            renderer,
        ],
    )

    root = logging.getLogger()
    if _console is not None:
        root.removeHandler(_console)
    handler = logging.StreamHandler(destination)
    handler.setFormatter(formatter)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    _console = handler

    # httpx logs one INFO line per request. At eighty concurrent sources that is
    # the whole log, and none of it says anything the crawl has not already
    # recorded in `requests`.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str = "catalogue") -> structlog.stdlib.BoundLogger:
    if not _configured:
        # A library import must not decide the process's logging, but a caller
        # that never configured should still get working output rather than
        # the "no handlers" warning.
        configure()
    return structlog.stdlib.get_logger(name)


def attach(handler: logging.Handler) -> logging.Handler:
    """Add a sink beside the console one, without disturbing it.

    This is what a display or a `PostgresSink` uses. It returns the handler so
    the caller can pass the same object back to `detach`.
    """
    logging.getLogger().addHandler(handler)
    return handler


def detach(handler: logging.Handler) -> None:
    """Remove exactly one previously attached sink."""
    logging.getLogger().removeHandler(handler)


@contextmanager
def quieten() -> Iterator[None]:
    """Silence the console handler while a full-screen display owns the terminal.

    Textual draws over the whole terminal, so a log line written to stderr
    corrupts the display. The old code solved that by replacing the handler
    list, which also silenced anything else that had been attached. Raising this
    one handler's level leaves every other sink — the database, a file, a log
    shipper — writing exactly as before.
    """
    if _console is None:
        yield
        return
    previous = _console.level
    _console.setLevel(logging.CRITICAL + 1)
    try:
        yield
    finally:
        _console.setLevel(previous)


def bind(**values: Any) -> None:
    """Put identifiers on every line emitted from here on in this task."""
    structlog.contextvars.bind_contextvars(**values)


def unbind(*keys: str) -> None:
    structlog.contextvars.unbind_contextvars(*keys)


@contextmanager
def bound(**values: Any) -> Iterator[None]:
    """Bind identifiers for the duration of a block.

    A source's task binds `source=` once here, and every line inside it — the
    scraper's, the fetcher's, the limiter's — carries it without a single call
    site passing it.
    """
    tokens = structlog.contextvars.bind_contextvars(**values)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)
