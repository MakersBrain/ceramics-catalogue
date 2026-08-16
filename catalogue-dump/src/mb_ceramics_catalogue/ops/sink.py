"""The Postgres progress sink: what makes a run watchable from a browser.

A `ProgressSink` like any other (see `crawl.progress`), so it composes with the
terminal ones rather than replacing them — a worker run with a TTY attached can
have both, which is only true because §4.6 stopped displays from swapping the
root logger's handler list.

Two things here are load-bearing under real load.

**Write amplification.** A source doing 3,000 requests must not do 3,000
progress updates. The counters are cumulative, so a dropped intermediate value
costs nothing and the writes are throttled to at most one per second per job and
coalesced. An 80-source run therefore writes ~80 rows a second at its peak,
against a table with one row per job.

**Levels never become edges.** Nothing in this module touches
`catalogue.event_log`. Progress goes to `job_progress`, updated in place, whose
own trigger notifies `catalogue_progress` with the job id. If progress were ever
written to the event log "for consistency", a three-hour run would put ~860,000
rows in it, replay would become unusable and reconnection would stop working.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import UUID

import psycopg
import structlog.contextvars
from psycopg.types.json import Jsonb

from mb_ceramics_catalogue.observability import logging as obs
from mb_ceramics_catalogue.ops import events
from mb_ceramics_catalogue.scrapers.activity import ACTIVITY, CURRENT_JOB, describe

LOGGER = obs.get_logger("catalogue.sink")

Connection = psycopg.AsyncConnection[dict[str, Any]]

#: Least time between two progress writes for one job.
THROTTLE_SECONDS = 1.0

# Only fields with a demonstrated operator use cross the database boundary.
# In particular, never persist the unrestricted LogRecord dictionary: third
# party libraries put response bodies and arbitrary objects there.
JOB_LOG_DATA_FIELDS = frozenset({"source", "scraper", "host", "request_id", "trace_id"})
JOB_LOG_VALUE_LIMIT = 512
JOB_LOG_EVENT_LIMIT = 128
JOB_LOG_MESSAGE_LIMIT = 4096

#: In-flight requests carried into `job_progress.in_flight`. The browser shows
#: what the terminal shows; forty would be a payload nobody reads.
IN_FLIGHT_LIMIT = 10

UPSERT_PROGRESS = """
insert into catalogue.job_progress
       (job_id, updated_at, phase, discovered, records, requests,
        rendered_pages, error_count, truncated, in_flight,
        http_tx_bytes_estimated, http_rx_bytes_estimated,
        browser_tx_bytes_estimated, browser_rx_bytes_estimated, cache_bytes_read,
        direct_requests, impersonated_requests, browser_requests, proxy_requests,
        proxy_bytes_reserved, proxy_bytes_estimated,
        browser_gain, browser_zero_gain, outcome_counts)
values (%(job_id)s, now(), %(phase)s, %(discovered)s, %(records)s, %(requests)s,
        %(rendered)s, %(errors)s, %(truncated)s, %(in_flight)s,
        %(http_tx)s, %(http_rx)s, %(browser_tx)s, %(browser_rx)s, %(cache_bytes)s,
        %(direct)s, %(impersonated)s, %(browser_requests)s, %(proxy_requests)s,
        %(proxy_reserved)s, %(proxy_estimated)s,
        %(browser_gain)s, %(browser_zero_gain)s, %(outcomes)s)
on conflict (job_id) do update
   set updated_at = now(),
       phase = excluded.phase,
       discovered = excluded.discovered,
       records = excluded.records,
       requests = excluded.requests,
       rendered_pages = excluded.rendered_pages,
       error_count = excluded.error_count,
       truncated = excluded.truncated,
       in_flight = excluded.in_flight,
       http_tx_bytes_estimated = excluded.http_tx_bytes_estimated,
       http_rx_bytes_estimated = excluded.http_rx_bytes_estimated,
       browser_tx_bytes_estimated = excluded.browser_tx_bytes_estimated,
       browser_rx_bytes_estimated = excluded.browser_rx_bytes_estimated,
       cache_bytes_read = excluded.cache_bytes_read,
       direct_requests = excluded.direct_requests,
       impersonated_requests = excluded.impersonated_requests,
       browser_requests = excluded.browser_requests,
       proxy_requests = excluded.proxy_requests,
       proxy_bytes_reserved = excluded.proxy_bytes_reserved,
       proxy_bytes_estimated = excluded.proxy_bytes_estimated,
       browser_gain = excluded.browser_gain,
       browser_zero_gain = excluded.browser_zero_gain,
       outcome_counts = excluded.outcome_counts
"""


class PostgresSink:
    """Writes a run's progress and log to the database.

    Constructed with the run and the source -> job id mapping, because one
    `catalogue-dump` process may be running eighty sources against eighty jobs
    while a worker runs exactly one.
    """

    def __init__(
        self,
        connection: Connection,
        run_id: UUID,
        jobs: dict[str, UUID],
        *,
        throttle: float = THROTTLE_SECONDS,
    ) -> None:
        self.connection = connection
        self.run_id = run_id
        self.jobs = jobs
        self.throttle = throttle
        self._last_write: dict[str, float] = {}
        self._phase: dict[str, str] = {}

    def job_for(self, source: str) -> UUID | None:
        return self.jobs.get(source)

    # -- ProgressSink -----------------------------------------------------

    async def started(self, source: str, result: Any, scraper: str = "", method: str = "") -> None:
        job_id = self.job_for(source)
        if job_id is None:
            return
        self._phase[source] = "discovering"
        await events.log(
            self.connection,
            job_id,
            f"started {source} with {scraper}",
            event="job.started",
            data={"scraper": scraper, "method": method},
        )
        await self._write(source, job_id, result, force=True)

    async def progress(self, source: str, result: Any) -> None:
        job_id = self.job_for(source)
        if job_id is None:
            return
        # Once anything has been collected the source is no longer discovering
        # what to fetch; the phase is what the UI colours a row by, and a bar
        # that says "discovering" for an hour is worse than no bar.
        if result.records and self._phase.get(source) == "discovering":
            self._phase[source] = "fetching"
        await self._write(source, job_id, result)

    async def finished(self, source: str, summary: dict[str, Any]) -> None:
        job_id = self.job_for(source)
        if job_id is None:
            return
        self._phase[source] = "loading"
        await self._upsert(
            job_id,
            phase="loading",
            discovered=summary.get("discovered", 0),
            records=summary.get("records", 0),
            requests=summary.get("requests", 0),
            rendered=summary.get("rendered_pages", 0),
            errors=summary.get("error_count", 0),
            truncated=bool(summary.get("truncated")),
            in_flight=[],
            http_tx=summary.get("http_tx_bytes_estimated", 0),
            http_rx=summary.get("http_rx_bytes_estimated", 0),
            browser_tx=summary.get("browser_tx_bytes_estimated", 0),
            browser_rx=summary.get("browser_rx_bytes_estimated", 0),
            cache_bytes=summary.get("cache_bytes_read", 0),
            direct=summary.get("direct_requests", 0),
            impersonated=summary.get("impersonated_requests", 0),
            browser_requests=summary.get("browser_requests", 0),
            proxy_requests=summary.get("proxy_requests", 0),
            browser_gain=summary.get("browser_gain", 0),
            browser_zero_gain=summary.get("browser_zero_gain", 0),
            outcomes=summary.get("outcome_counts", {}),
        )
        await events.log(
            self.connection,
            job_id,
            f"collected {summary.get('records', 0)} records from {source}",
            event="job.collected",
            level="warning" if summary.get("error_count") else "info",
            data={
                "records": summary.get("records"),
                "requests": summary.get("requests"),
                "errors": summary.get("error_count"),
                "truncated": summary.get("truncated"),
            },
        )

    async def close(self) -> None:
        return None

    # -- writing ----------------------------------------------------------

    async def _write(self, source: str, job_id: UUID, result: Any, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_write.get(source, 0.0) < self.throttle:
            return
        self._last_write[source] = now
        await self._upsert(
            job_id,
            phase=self._phase.get(source, "fetching"),
            discovered=getattr(result, "discovered", 0),
            records=len(getattr(result, "records", [])),
            requests=getattr(result, "requests", 0),
            rendered=getattr(result, "rendered_pages", 0),
            errors=len(getattr(result, "errors", [])),
            truncated=bool(getattr(result, "truncated", False)),
            in_flight=self._in_flight(source),
            http_tx=getattr(result, "http_tx_bytes_estimated", 0),
            http_rx=getattr(result, "http_rx_bytes_estimated", 0),
            browser_tx=getattr(result, "browser_tx_bytes_estimated", 0),
            browser_rx=getattr(result, "browser_rx_bytes_estimated", 0),
            cache_bytes=getattr(result, "cache_bytes_read", 0),
            direct=getattr(result, "direct_requests", 0),
            impersonated=getattr(result, "impersonated_requests", 0),
            browser_requests=getattr(result, "browser_requests", 0),
            proxy_requests=getattr(result, "proxy_requests", 0),
            proxy_reserved=getattr(result, "proxy_bytes_reserved", 0),
            proxy_estimated=getattr(result, "proxy_bytes_estimated", 0),
            browser_gain=getattr(result, "browser_gain", 0),
            browser_zero_gain=getattr(result, "browser_zero_gain", 0),
            outcomes=getattr(result, "outcome_counts", {}),
        )

    @staticmethod
    def _in_flight(source: str) -> list[dict[str, Any]]:
        """The requests this source has outstanding, as the Textual view shows them.

        `scrapers.activity` already tracks this for the terminal display, so the
        browser gets the same answer to "what is that source waiting on" for
        free — and it is the single most useful thing on the page during a run
        that has gone quiet.
        """
        return [
            {"seconds": round(age, 1), "url": describe(url, 200)}
            for age, url in ACTIVITY.in_flight(source)[:IN_FLIGHT_LIMIT]
        ]

    async def _upsert(self, job_id: UUID, **values: Any) -> None:
        parameters = {"job_id": job_id, **values}
        parameters["in_flight"] = Jsonb(values.get("in_flight") or [])
        parameters["outcomes"] = Jsonb(values.get("outcomes") or {})
        try:
            async with self.connection.cursor() as cursor:
                await cursor.execute(UPSERT_PROGRESS, parameters)
        except psycopg.Error:
            # A sink is never worth a run. The counters are cumulative, so the
            # next tick carries everything this write would have said.
            LOGGER.debug("sink.progress_write_failed", job_id=str(job_id), exc_info=True)


class JobLogHandler(logging.Handler):
    """Relays log records into `catalogue.job_events` for the job detail page.

    An additional handler, never a replacement — the same trick
    `interactive.LogRelay` already uses, and the reason the worker keeps writing
    structured JSON to stdout while also filling the database.

    Records are queued rather than written inline: `emit` is called from
    synchronous logging code inside the event loop's thread, and awaiting a
    database round trip there would make every log call a scheduling point.

    A handler on the root logger is offered *every* record in the process, and a
    worker with four job slots has four of these attached at once. Without the
    `CURRENT_JOB` check below, each job's log page showed all four jobs' lines —
    with the other jobs' ids inside the messages — which is at its most
    misleading exactly when someone is reading it to find out why a job failed.
    """

    def __init__(self, job_id: UUID, level: int = logging.INFO, capacity: int = 2000) -> None:
        super().__init__(level)
        self.job_id = job_id
        self.capacity = capacity
        self.pending: list[tuple[str, str, str, dict[str, Any] | None]] = []
        self.dropped = 0
        self._key = str(job_id)

    def emit(self, record: logging.LogRecord) -> None:
        # Read in `emit`, not in `flush_to`: emit runs synchronously in the task
        # that logged, which is the only place the answer is still known. The
        # flush happens later, from the worker's own task, where every record
        # would look like it came from whichever job was current then.
        if CURRENT_JOB.get() != self._key:
            return
        try:
            raw = record.msg if isinstance(record.msg, dict) else {}
            message = str(raw.get("event")) if raw.get("event") is not None else record.getMessage()
        except Exception:  # noqa: BLE001 - a bad log line must not stop a crawl
            return
        if len(self.pending) >= self.capacity:
            # Drop rather than grow without bound. A job that produced two
            # thousand log lines has a problem the next two thousand will not
            # explain any better.
            self.dropped += 1
            return
        # This handler sees records before ProcessorFormatter performs console
        # redaction. Apply the same scrubber at the durable boundary, and bound
        # both free-text columns so one foreign log record cannot dominate the
        # database despite the queue's record-count limit.
        message = str(obs.scrub(message))[:JOB_LOG_MESSAGE_LIMIT]
        event = str(obs.scrub(raw.get("event") or getattr(record, "event", None) or record.name))[
            :JOB_LOG_EVENT_LIMIT
        ]
        context = structlog.contextvars.get_contextvars()
        data: dict[str, Any] = {}
        for key in JOB_LOG_DATA_FIELDS:
            value = raw.get(key, getattr(record, key, context.get(key)))
            if value is not None:
                data[key] = _bounded_log_value(obs.scrub(value))
        self.pending.append((_level_name(record.levelno), event, message, data or None))

    def drain(self) -> list[tuple[str, str, str, dict[str, Any] | None]]:
        queued, self.pending = self.pending, []
        if self.dropped:
            queued.append(
                ("warning", "job.log_truncated", f"{self.dropped} further log lines dropped", None)
            )
            self.dropped = 0
        return queued

    async def flush_to(self, connection: Connection) -> int:
        rows = self.drain()
        written = 0
        for index, (level, event, message, data) in enumerate(rows):
            try:
                await events.log(
                    connection, self.job_id, message, level=level, event=event, data=data
                )
            except psycopg.Error:
                # Put the failed row and everything after it back in front of any
                # records emitted while this flush was awaiting the database.
                restored = rows[index:] + self.pending
                self.pending = restored[: self.capacity]
                self.dropped += len(restored) - len(self.pending)
                LOGGER.debug("sink.log_write_failed", job_id=str(self.job_id), exc_info=True)
                break
            written += 1
        return written


def _bounded_log_value(value: Any) -> Any:
    if isinstance(value, str):
        return value[:JOB_LOG_VALUE_LIMIT]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    # All allowlisted fields are scalar today. Stringifying an unexpected value
    # keeps the JSON bounded without letting an arbitrary object graph through.
    return str(value)[:JOB_LOG_VALUE_LIMIT]


def _level_name(levelno: int) -> str:
    if levelno >= logging.ERROR:
        return "error"
    if levelno >= logging.WARNING:
        return "warning"
    if levelno >= logging.INFO:
        return "info"
    return "debug"
