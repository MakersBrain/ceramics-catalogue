from __future__ import annotations

import io
import json
import logging
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import structlog.contextvars

from mb_ceramics_catalogue.observability import logging as obs
from mb_ceramics_catalogue.observability import metrics, tracing
from mb_ceramics_catalogue.observability.http import RequestTelemetry, request_id
from mb_ceramics_catalogue.ops import sink as sink_module
from mb_ceramics_catalogue.ops.sink import JOB_LOG_VALUE_LIMIT, JobLogHandler
from mb_ceramics_catalogue.scrapers.activity import CURRENT_JOB


def test_active_trace_id_is_added_to_structured_logs(monkeypatch) -> None:
    destination = io.StringIO()
    monkeypatch.setattr(tracing, "trace_id", lambda: "abc123")
    obs.configure(json=True, stream=destination)

    obs.get_logger("test").info("job.started")

    assert json.loads(destination.getvalue())["trace_id"] == "abc123"


def test_job_log_handler_persists_only_bounded_allowlisted_context() -> None:
    job_id = uuid4()
    handler = JobLogHandler(job_id)
    CURRENT_JOB.set(str(job_id))
    tokens = structlog.contextvars.bind_contextvars(
        source="ceradel",
        host="example.test",
        request_id="r" * (JOB_LOG_VALUE_LIMIT + 20),
        ignored="must not cross the database boundary",
    )
    try:
        record = logging.LogRecord(
            "catalogue.test",
            logging.INFO,
            __file__,
            1,
            {"event": "request.finished", "scraper": "shopify", "body": "secret body"},
            None,
            None,
        )
        handler.emit(record)
    finally:
        structlog.contextvars.reset_contextvars(**tokens)

    [(level, event, message, data)] = handler.drain()
    assert (level, event, message) == ("info", "request.finished", "request.finished")
    assert data == {
        "host": "example.test",
        "request_id": "r" * JOB_LOG_VALUE_LIMIT,
        "scraper": "shopify",
        "source": "ceradel",
    }


def test_database_log_payload_uses_the_secret_scrubber() -> None:
    job_id = uuid4()
    handler = JobLogHandler(job_id)
    CURRENT_JOB.set(str(job_id))
    obs.register_secrets({"credential-value"})
    tokens = structlog.contextvars.bind_contextvars(
        host="https://name:credential-value@example.test/path"
    )
    try:
        handler.emit(
            logging.LogRecord("catalogue.test", logging.INFO, __file__, 1, "request", None, None)
        )
    finally:
        structlog.contextvars.reset_contextvars(**tokens)

    assert handler.drain()[0][3] == {"host": "https://[REDACTED]@example.test/path"}


async def test_failed_log_flush_keeps_a_bounded_retry_buffer(monkeypatch) -> None:
    job_id = uuid4()
    handler = JobLogHandler(job_id, capacity=2)
    CURRENT_JOB.set(str(job_id))
    handler.emit(logging.LogRecord("catalogue.test", logging.INFO, __file__, 1, "one", None, None))
    handler.emit(logging.LogRecord("catalogue.test", logging.INFO, __file__, 1, "two", None, None))
    connection: Any = object()

    async def unavailable(*args, **kwargs) -> None:
        raise psycopg.OperationalError("database unavailable")

    monkeypatch.setattr(sink_module.events, "log", unavailable)
    assert await handler.flush_to(connection) == 0
    assert len(handler.pending) == 2

    persisted: list[str] = []

    async def available(connection, job, message, **kwargs) -> None:
        persisted.append(message)

    monkeypatch.setattr(sink_module.events, "log", available)
    assert await handler.flush_to(connection) == 2
    assert persisted == ["one", "two"]


def test_request_id_rejects_unbounded_or_unsafe_values() -> None:
    assert request_id("safe-id:42") == "safe-id:42"
    assert request_id("contains spaces") != "contains spaces"
    assert len(request_id("x" * 129)) == 36


async def test_request_telemetry_uses_route_templates_and_echoes_request_id() -> None:
    metrics.REGISTRY.clear()

    async def endpoint(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    route = SimpleNamespace(endpoint=endpoint, path="/v1/jobs/{id}")
    router = SimpleNamespace(routes=[route])

    async def app(scope, receive, send) -> None:
        scope["endpoint"] = endpoint
        scope["router"] = router
        await endpoint(scope, receive, send)

    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "path": "/v1/jobs/one-specific-id",
        "method": "GET",
        "headers": [(b"x-request-id", b"test-request")],
    }
    await RequestTelemetry(app, "control")(scope, receive, send)

    assert (b"x-request-id", b"test-request") in sent[0]["headers"]
    rendered = metrics.render()
    assert 'route="/v1/jobs/{id}"' in rendered
    assert "one-specific-id" not in rendered
    assert 'status_class="2xx"' in rendered
    assert 'catalogue_http_requests_in_flight{service="control"} 0' in rendered


def test_database_snapshot_gauges_remove_old_series_and_emit_zero_states() -> None:
    metrics.REGISTRY.clear()
    metrics.jobs_snapshot({"queued": 3})
    metrics.workers_snapshot(2, 1)
    metrics.sources_snapshot(
        [{"source": "old", "overdue": 4, "succeeded": 1, "records": 10, "record_ratio": 1.0}]
    )

    metrics.jobs_snapshot({})
    metrics.workers_snapshot(1, 0)
    metrics.sources_snapshot(
        [{"source": "new", "overdue": 0, "succeeded": 0, "records": None, "record_ratio": None}]
    )
    rendered = metrics.render()

    assert 'catalogue_jobs{state="queued"} 0' in rendered
    assert 'catalogue_workers{health="healthy"} 1' in rendered
    assert 'catalogue_workers{health="lost"} 0' in rendered
    assert 'source="old"' not in rendered
    assert 'catalogue_source_success_state{source="new"} 0' in rendered
