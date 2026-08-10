"""`catalogue-control`: the write path, kept out of the read API on purpose.

`catalogue-service`'s docstring states its contract plainly: "there is no write
path here at all rather than a write path behind a permission." Putting run
control in a separate service preserves that property rather than arguing with
it — a tenant reading the catalogue cannot reach a cancel-run endpoint, because
it is not in the service they can reach.

Every `/v1` route, including the stream, requires a bearer token. `/health` and
`/metrics` are the only exemptions, and the service is additionally not
published on the host — which is defence in depth, not the authentication
boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import psycopg
from ateliera_catalogue.config.settings import CrawlParams
from ateliera_catalogue.config.sources import SourcesFile, default_path
from ateliera_catalogue.ops import events, runs
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from catalogue_control import queries
from catalogue_control.broker import Broker, Subscriber, parse_topics
from catalogue_control.settings import Settings
from catalogue_control.telemetry import get_logger, render_metrics

LOGGER = get_logger("catalogue.control")

#: SSE keepalive. Without it an idle proxy closes the connection after a minute
#: and the browser reconnects in a loop it never reports.
KEEPALIVE_SECONDS = 15


def problem(status: int, title: str, detail: str | None = None, kind: str = "about:blank") -> Response:
    """RFC 9457 `application/problem+json`.

    `{"error": "..."}` was an undocumented string that every client had to
    guess at. One error schema, referenced from every operation, is the thing
    that makes the generated spec worth reading (§10.3).
    """
    body: dict[str, Any] = {"type": kind, "title": title, "status": status}
    if detail:
        body["detail"] = detail
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


class BearerToken:
    """Default deny on `/v1`, including the stream.

    Raw ASGI middleware rather than `BaseHTTPMiddleware`, and that is not a
    style choice: `BaseHTTPMiddleware` consumes a `StreamingResponse` through an
    intermediate task and does not forward it incrementally, so wrapping the app
    in one makes `/v1/events` deliver nothing until the stream ends — which, for
    a stream designed to stay open for hours, means never.

    The symptom is the worst kind: every JSON route works, the SSE handshake
    succeeds, and the browser simply sits there receiving no events.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/v1"):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        header = headers.get("authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else ""
        # A query-string token is deliberately not accepted, even though
        # `EventSource` cannot set headers: it would land in access logs and in
        # `Referer` headers. The explorer proxies the stream through a
        # SvelteKit route that adds the header server-side instead (§6.5).
        if not supplied or not _constant_time_equal(supplied, self.token):
            response = problem(401, "Unauthorized", "a bearer token is required on /v1 routes")
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _constant_time_equal(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left.encode(), right.encode())


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def health(request: Request) -> Response:
    try:
        async with request.app.state.pool.connection() as connection:
            await connection.execute("select 1")
    except psycopg.Error:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok"})


async def metrics_endpoint(request: Request) -> Response:
    """Prometheus text. Live gauges are read from the database, not cached.

    The queue depth is authoritative in Postgres and cheap to count; keeping a
    process-local copy would mean each replica reported a different number.
    """
    from ateliera_catalogue.observability import metrics as instruments

    with contextlib.suppress(psycopg.Error):
        async with request.app.state.pool.connection() as connection:
            for state, count in (await queries.queue_depth(connection)).items():
                instruments.jobs(state, count)
            for row in await queries.all_rows(connection, queries.WORKERS):
                instruments.worker_heartbeat_age(
                    str(row["id"]), float(row["heartbeat_age_seconds"] or 0)
                )
            for row in await queries.all_rows(connection, queries.SOURCES):
                if row["staleness_seconds"] is not None:
                    instruments.source_staleness(row["source_id"], float(row["staleness_seconds"]))
    return PlainTextResponse(render_metrics(), media_type="text/plain; version=0.0.4")


async def create_run(request: Request) -> Response:
    body = await _json(request)
    if body is None:
        return problem(400, "Bad Request", "a JSON object is required")

    sources: SourcesFile = request.app.state.sources
    try:
        # The same model the CLI and the scheduler validate against, so a run
        # created here cannot mean something a run created there would not.
        params = CrawlParams.model_validate(body.get("params") or {})
    except Exception as error:  # noqa: BLE001 - pydantic's message is the useful part
        return problem(422, "Invalid run parameters", str(error))

    try:
        selected = sources.select(body.get("sources") or "all")
    except ValueError as error:
        return problem(422, "Unknown source", str(error))

    async with request.app.state.pool.connection() as connection:
        run_id = await runs.create_run(
            connection,
            kind=body.get("kind", "manual"),
            requested_by=body.get("requested_by"),
            params=params.model_dump(mode="json"),
        )
        if run_id is None:
            return problem(409, "Conflict", "this scheduled occurrence already exists")
        jobs = await runs.create_jobs(connection, run_id, sources, selected)

    return JSONResponse(
        {"run_id": str(run_id), "jobs": len(jobs), "sources": sorted(jobs)}, status_code=202
    )


async def list_runs(request: Request) -> Response:
    limit = _limit(request, default=25, maximum=200)
    async with request.app.state.pool.connection() as connection:
        rows = await queries.all_rows(
            connection, queries.RUNS, {"limit": limit, "cursor": request.query_params.get("cursor")}
        )
    next_cursor = rows[-1]["created_at"].isoformat() if len(rows) == limit else None
    return _json_response({"runs": rows, "next_cursor": next_cursor})


async def get_run(request: Request) -> Response:
    run_id = _uuid(request, "id")
    if run_id is None:
        return problem(400, "Bad Request", "id must be a uuid")
    async with request.app.state.pool.connection() as connection:
        run = await queries.one(connection, queries.RUN, {"id": run_id})
        if run is None:
            return problem(404, "Not Found", "no such run")
        jobs = await queries.all_rows(connection, queries.RUN_JOBS, {"run": run_id})
    return _json_response({"run": run, "jobs": jobs})


async def cancel_run(request: Request) -> Response:
    run_id = _uuid(request, "id")
    if run_id is None:
        return problem(400, "Bad Request", "id must be a uuid")
    async with request.app.state.pool.connection() as connection:
        cancelled = await queries.all_rows(connection, queries.CANCEL_RUN, {"run": run_id})
        for row in cancelled:
            await events.emit(
                connection, events.Topic.JOB, "job.cancel_requested",
                run_id=run_id, job_id=row["id"], source_id=row["source_id"],
            )
    return JSONResponse({"cancelled": len(cancelled)}, status_code=202)


async def job_action(request: Request) -> Response:
    """pause, resume, cancel or retry one source.

    Four separate controls rather than one, because they have different safety
    properties: a pause keeps the job resumable and spends no attempt, a cancel
    is terminal and keeps the partial artifact, and a retry is a new attempt the
    operator explicitly asked for.
    """
    job_id = _uuid(request, "id")
    action = request.path_params["action"]
    statements = {
        "pause": queries.PAUSE_JOB,
        "resume": queries.RESUME_JOB,
        "cancel": queries.CANCEL_JOB,
        "retry": queries.RETRY_JOB,
    }
    if job_id is None:
        return problem(400, "Bad Request", "id must be a uuid")
    if action not in statements:
        return problem(404, "Not Found", f"unknown action {action!r}")

    async with request.app.state.pool.connection() as connection:
        row = await queries.one(connection, statements[action], {"id": job_id})
        if row is None:
            # Conditional on the current state, so this is "not in a state where
            # that means anything" rather than a failure. Pressing the same
            # button twice is a no-op.
            return problem(409, "Conflict", f"this job cannot be {action}ed in its current state")
        await events.emit(
            connection, events.Topic.JOB, f"job.{action}_requested",
            run_id=row["run_id"], job_id=job_id, source_id=row["source_id"],
        )
    return JSONResponse({"job_id": str(job_id), "action": action}, status_code=202)


async def job_logs(request: Request) -> Response:
    job_id = _uuid(request, "id")
    if job_id is None:
        return problem(400, "Bad Request", "id must be a uuid")
    async with request.app.state.pool.connection() as connection:
        rows = await queries.all_rows(
            connection,
            queries.JOB_LOG,
            {
                "job": job_id,
                "after": int(request.query_params.get("after", 0) or 0),
                "level": request.query_params.get("level"),
                "search": request.query_params.get("q"),
                "limit": _limit(request, default=500, maximum=2000),
            },
        )
    return _json_response({"lines": rows, "next_after": rows[-1]["id"] if rows else None})


async def get_job(request: Request) -> Response:
    job_id = _uuid(request, "id")
    if job_id is None:
        return problem(400, "Bad Request", "id must be a uuid")
    async with request.app.state.pool.connection() as connection:
        row = await queries.one(connection, queries.JOB, {"id": job_id})
    if row is None:
        return problem(404, "Not Found", "no such job")
    return _json_response({"job": row})


async def list_workers(request: Request) -> Response:
    """The roster, in exactly the shape the stream pushes it.

    Shaped through the same `_worker` projection the `worker.roster` event uses.
    They were two hand-written shapes that differed only in calling the
    identifier `id` here and `worker_id` there, which is precisely the kind of
    difference that makes a client merge two records into one worker and show
    the fleet as twice its size.
    """
    from catalogue_control.broker import _worker

    async with request.app.state.pool.connection() as connection:
        rows = await queries.all_rows(connection, queries.WORKERS)
    return _json_response(
        {
            "workers": [
                {**_worker(row), "heartbeat_age_seconds": row["heartbeat_age_seconds"]}
                for row in rows
            ]
        }
    )


async def worker_action(request: Request) -> Response:
    """Pause, resume, drain or stop a worker, or hide a lost registration.

    This controls the registered process, not the deployment's replica count. A
    restart policy may well create a new worker afterwards, so persistently
    removing capacity is a scale operation and this API does not pretend
    otherwise.
    """
    worker_id = _uuid(request, "id")
    action = request.path_params["action"]
    desired = {"pause": "paused", "resume": "running", "drain": "draining", "stop": "stopping"}
    if worker_id is None:
        return problem(400, "Bad Request", "id must be a uuid")
    if action not in {*desired, "hide"}:
        return problem(404, "Not Found", f"unknown action {action!r}")

    async with request.app.state.pool.connection() as connection:
        if action == "hide":
            row = await queries.one(connection, queries.HIDE_LOST_WORKER, {"id": worker_id})
            if row is None:
                return problem(409, "Conflict", "only a lost worker can be hidden")
            await events.emit(
                connection, events.Topic.WORKER, "worker.changed",
                worker_id=worker_id, payload={"status": "stopped", "hidden": True},
            )
            return JSONResponse(
                {"worker_id": str(worker_id), "status": "stopped", "hidden": True},
                status_code=202,
            )

        row = await queries.one(
            connection, queries.SET_WORKER_STATE, {"id": worker_id, "desired": desired[action]}
        )
        if row is None:
            return problem(409, "Conflict", "no such worker, or it has already stopped")
        await events.emit(
            connection, events.Topic.WORKER, "worker.changed",
            worker_id=worker_id, payload={"desired_state": desired[action]},
        )
    return JSONResponse({"worker_id": str(worker_id), "desired_state": desired[action]}, status_code=202)


async def list_sources(request: Request) -> Response:
    """sources.json joined to what actually happened to each source."""
    sources: SourcesFile = request.app.state.sources
    async with request.app.state.pool.connection() as connection:
        observed = {row["source_id"]: row for row in await queries.all_rows(connection, queries.SOURCES)}

    payload = []
    for name, config in sources.items():
        row = observed.get(name, {})
        last = row.get("last_records")
        previous = row.get("previous_records")
        payload.append(
            {
                "source_id": name,
                "label": config.label,
                "url": config.url,
                "scraper": config.scraper,
                "country": config.country,
                "enabled": row.get("enabled", True),
                "paused": row.get("paused", False),
                "schedule_id": row.get("schedule_id"),
                "params": row.get("params", {}),
                "last_success_at": row.get("last_success_at"),
                "last_records": last,
                "previous_records": previous,
                # The single most useful number on the page: a source that has
                # quietly halved is the failure this whole plan exists to catch.
                "delta": (last - previous) if last is not None and previous is not None else None,
                "staleness_seconds": row.get("staleness_seconds"),
                "runs_7d": row.get("runs_7d", 0),
                "failures_7d": row.get("failures_7d", 0),
            }
        )
    return _json_response({"sources": payload})


async def update_source(request: Request) -> Response:
    name = request.path_params["id"]
    sources: SourcesFile = request.app.state.sources
    if name not in sources:
        return problem(404, "Not Found", f"unknown source {name!r}")
    body = await _json(request) or {}

    if body.get("params"):
        try:
            CrawlParams.model_validate(body["params"])
        except Exception as error:  # noqa: BLE001
            return problem(422, "Invalid source parameters", str(error))

    async with request.app.state.pool.connection() as connection:
        row = await queries.one(
            connection,
            queries.UPSERT_SOURCE,
            {
                "id": name,
                "enabled": bool(body.get("enabled", True)),
                "paused": bool(body.get("paused", False)),
                "schedule": body.get("schedule_id"),
                "params": queries.as_jsonb(body.get("params")),
                "by": body.get("updated_by"),
            },
        )
        if body.get("paused"):
            # Pausing a source also pauses the jobs it already has in flight.
            # Resuming does not automatically resume individually paused jobs.
            await queries.all_rows(connection, queries.PAUSE_SOURCE_JOBS, {"id": name})
        await events.emit(
            connection, events.Topic.SOURCE, "source.changed", source_id=name, payload=dict(body)
        )
    return _json_response({"source": row})


async def list_notifications(request: Request) -> Response:
    async with request.app.state.pool.connection() as connection:
        rows = await queries.all_rows(
            connection,
            queries.NOTIFICATIONS,
            {
                "unacknowledged": request.query_params.get("unacknowledged") == "true",
                "severity": request.query_params.get("severity"),
                "limit": _limit(request, default=100, maximum=500),
            },
        )
    return _json_response({"notifications": rows})


async def acknowledge_notification(request: Request) -> Response:
    try:
        notification_id = int(request.path_params["id"])
    except ValueError:
        return problem(400, "Bad Request", "id must be a number")
    body = await _json(request) or {}
    async with request.app.state.pool.connection() as connection:
        done = await events.acknowledge(connection, notification_id, body.get("by") or "operator")
    if not done:
        return problem(409, "Conflict", "already acknowledged, or no such notification")
    return JSONResponse({"id": notification_id, "acknowledged": True})


async def list_schedules(request: Request) -> Response:
    async with request.app.state.pool.connection() as connection:
        rows = await queries.all_rows(connection, queries.SCHEDULES)
    return _json_response({"schedules": rows})


async def update_schedule(request: Request) -> Response:
    body = await _json(request) or {}
    name = request.path_params["id"]
    async with request.app.state.pool.connection() as connection:
        row = await queries.one(
            connection,
            queries.UPSERT_SCHEDULE,
            {
                "id": name,
                "enabled": bool(body.get("enabled", True)),
                "cron": body.get("cron", "0 3 * * *"),
                "timezone": body.get("timezone", "Europe/Paris"),
                "filter": queries.as_jsonb(body.get("source_filter") or {"all": True}),
                "params": queries.as_jsonb(body.get("params")),
            },
        )
        await events.emit(
            connection, events.Topic.SCHEDULE, "schedule.changed", payload={"id": name}
        )
    return _json_response({"schedule": row})


# ---------------------------------------------------------------------------
# The stream
# ---------------------------------------------------------------------------


async def stream(request: Request) -> Response:
    """One multiplexed stream, filtered by topic.

    One endpoint rather than one per concern, because HTTP/1.1 caps a browser at
    roughly six connections per origin: three `EventSource` objects per tab
    means two tabs deadlock the app against its own streams.
    """
    broker: Broker = request.app.state.broker
    topics = parse_topics(request.query_params.get("topics"))
    run_id = request.query_params.get("run_id")

    last_event_id: int | None = None
    header = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
    if header:
        with contextlib.suppress(ValueError):
            last_event_id = int(header)

    async def body() -> AsyncIterator[bytes]:
        async with request.app.state.pool.connection() as connection:
            snapshot = await queries.bootstrap(
                connection, UUID(run_id) if run_id and "progress" in topics else None
            )
        # The stream opens with everything needed to render the first frame, so
        # a client never has to make a second request to draw anything.
        yield _sse("bootstrap", {**snapshot, "watermark": broker.watermark}).encode()

        agen = broker.subscribe(topics, run_id, last_event_id)
        subscriber: Subscriber = await agen.__anext__()
        try:
            while True:
                try:
                    message = await asyncio.wait_for(subscriber.queue.get(), KEEPALIVE_SECONDS)
                except TimeoutError:
                    # A comment, not an event: it keeps proxies from closing an
                    # idle connection without appearing in the client's handler.
                    yield b": keepalive\n\n"
                    continue
                if message is None:
                    return
                yield message.encode().encode()
                if subscriber.resync:
                    return
        finally:
            with contextlib.suppress(StopAsyncIteration):
                await agen.__anext__()

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # nginx and Caddy will otherwise buffer the stream into blocks and
            # it arrives in bursts, or not at all. Caddy additionally needs
            # `flush_interval -1` on the reverse_proxy.
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


# ---------------------------------------------------------------------------
# Helpers and wiring
# ---------------------------------------------------------------------------


async def _json(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body is a 400, not a 500
        return None
    return body if isinstance(body, dict) else None


def _uuid(request: Request, name: str) -> UUID | None:
    try:
        return UUID(request.path_params[name])
    except (ValueError, KeyError):
        return None


def _limit(request: Request, *, default: int, maximum: int) -> int:
    try:
        return min(int(request.query_params.get("limit", default)), maximum)
    except ValueError:
        return default


def _json_response(payload: dict[str, Any]) -> Response:
    return Response(
        json.dumps(payload, default=str), media_type="application/json"
    )


def create_app(settings: Settings | None = None) -> Starlette:
    settings = settings or Settings()
    if settings.require_token and not settings.control_token:
        raise ValueError(
            "CATALOGUE_CONTROL_TOKEN is not set. This service can cancel runs and "
            "disable sources; it refuses to start without authentication."
        )

    routes = [
        Route("/health", health),
        Route("/metrics", metrics_endpoint),
        Route("/v1/runs", create_run, methods=["POST"]),
        Route("/v1/runs", list_runs, methods=["GET"]),
        Route("/v1/runs/{id}", get_run),
        Route("/v1/runs/{id}/cancel", cancel_run, methods=["POST"]),
        Route("/v1/jobs/{id}", get_job),
        Route("/v1/jobs/{id}/logs", job_logs),
        Route("/v1/jobs/{id}/{action}", job_action, methods=["POST"]),
        Route("/v1/workers", list_workers),
        Route("/v1/workers/{id}/{action}", worker_action, methods=["POST"]),
        Route("/v1/sources", list_sources),
        Route("/v1/sources/{id}", update_source, methods=["PUT"]),
        Route("/v1/schedules", list_schedules),
        Route("/v1/schedules/{id}", update_schedule, methods=["PUT"]),
        Route("/v1/notifications", list_notifications),
        Route("/v1/notifications/{id}/ack", acknowledge_notification, methods=["POST"]),
        Route("/v1/events", stream),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        from ateliera_catalogue.storage import db

        async with db.pool(settings.dsn, minimum=1, maximum=8) as pool:
            app.state.pool = pool
            app.state.settings = settings
            app.state.sources = SourcesFile.load(default_path())
            app.state.broker = Broker(settings)
            await app.state.broker.start()
            try:
                yield
            finally:
                await app.state.broker.stop()

    return Starlette(
        routes=routes,
        lifespan=lifespan,
        middleware=[Middleware(BearerToken, token=settings.control_token)],
    )
