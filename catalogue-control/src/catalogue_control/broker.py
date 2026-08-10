"""The live stream: one LISTEN connection, many subscribers.

This is the part of the control service with real design in it, and almost all
of it is about not losing an event and not drowning in one.

**LISTEN is a hint, never the queue.** A `notify` payload carries only an id, and
`notify` itself is fire-and-forget: a listener that is reconnecting when one
fires loses it permanently. So the broker keeps a **watermark** — the greatest
event id it has dispatched — and every notification, every reconnect and a
five-second timer all trigger the same query for rows above it. A lost
notification therefore costs at most one catch-up interval of latency, even if
it was the last event of the run and every browser stayed connected. That hole
is otherwise permanent, and it is the one people discover in production.

**Edges are numbered; levels are not.** `event_log` rows carry an SSE `id:` and
can be replayed from `Last-Event-ID`. `job.progress` is a level: it is
deliberately unnumbered, never replayed, and re-snapshotted on bootstrap. A
client that missed forty progress readings does not want them.

**Backpressure drops the right things.** Per-subscriber queues are bounded. On
overflow, progress goes first — it is a level, and the next one carries
everything the dropped one said. If a durable event would be lost, the
subscriber is sent `resync` and disconnected rather than being silently served
an incomplete history.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.rows import dict_row

from catalogue_control.settings import Settings
from catalogue_control.telemetry import get_logger

LOGGER = get_logger("catalogue.control.broker")

#: Events kept in memory for a reconnecting client. A latency optimisation over
#: `catalogue.event_log`, never the source of truth — which is what lets more
#: than one control replica run: ids come from one Postgres sequence, so a
#: client that reconnects to a *different* replica still resumes correctly, at
#: worst by that replica reading the rows.
BUFFER_SIZE = 1000

#: Beyond this, replaying is worse than refetching. The client gets `resync` and
#: reloads state over the ordinary JSON endpoints.
MAX_REPLAY = 500

#: The watermark reconciliation interval. Closes the hole a dropped `notify`
#: would otherwise leave open for ever.
RECONCILE_SECONDS = 5.0

#: The worker roster is a level and is pushed on a timer, not as an event. A
#: worker heartbeating every 5s must not emit an event every 5s, and a worker
#: that silently died emits nothing at all — so the browser is given
#: `last_heartbeat_at` and derives the age locally.
ROSTER_SECONDS = 5.0

#: Progress is coalesced per subscriber in this window, keyed on job id, latest
#: wins. Lossless by construction: the counters are cumulative.
COALESCE_SECONDS = 0.5

#: Per-subscriber queue bound.
QUEUE_LIMIT = 256

ALL_TOPICS = ("workers", "runs", "jobs", "progress", "notifications", "schedules", "sources")

#: Omitting `topics` subscribes to everything except progress, which is the
#: expensive one and should be asked for deliberately.
DEFAULT_TOPICS = tuple(topic for topic in ALL_TOPICS if topic != "progress")

#: `event_log.topic` -> the stream topic a subscriber asks for.
TOPIC_OF = {
    "run": "runs",
    "job": "jobs",
    "worker": "workers",
    "notification": "notifications",
    "schedule": "schedules",
    "source": "sources",
}


@dataclass
class Message:
    """One thing to send. `event_id` is None for levels, which are not replayed."""

    event: str
    data: dict[str, Any]
    topic: str
    event_id: int | None = None
    run_id: str | None = None
    job_id: str | None = None

    def encode(self) -> str:
        lines = []
        if self.event_id is not None:
            lines.append(f"id: {self.event_id}")
        lines.append(f"event: {self.event}")
        lines.append(f"data: {json.dumps(self.data, default=str)}")
        return "\n".join(lines) + "\n\n"


# `eq=False` so instances hash by identity. A dataclass with the default
# generated `__eq__` is unhashable, and the broker keeps subscribers in a set —
# two clients with the same topics are two clients, not one.
@dataclass(eq=False)
class Subscriber:
    """One connected client."""

    topics: frozenset[str]
    run_id: str | None = None
    queue: asyncio.Queue[Message | None] = field(
        default_factory=lambda: asyncio.Queue(maxsize=QUEUE_LIMIT)
    )
    #: Latest progress per job, flushed on the coalescing tick.
    pending_progress: dict[str, Message] = field(default_factory=dict)
    resync: bool = False

    def wants(self, message: Message) -> bool:
        if message.topic not in self.topics:
            return False
        # A page watching one run does not want the other seventy-nine jobs.
        if self.run_id is not None and message.topic in ("jobs", "progress"):
            return message.run_id == self.run_id
        return True

    def offer(self, message: Message) -> None:
        """Queue a message, dropping levels before edges when full."""
        if message.topic == "progress":
            # Coalesced rather than queued: latest wins, keyed on job.
            if message.job_id:
                self.pending_progress[message.job_id] = message
            return
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            # Progress is already out of this queue, so an overflow here means
            # durable events would be lost. Say so and disconnect rather than
            # silently serving an incomplete history.
            self.resync = True
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(
                    Message(event="resync", data={"reason": "subscriber fell behind"}, topic="control")
                )

    def flush_progress(self) -> None:
        for message in self.pending_progress.values():
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(message)
        self.pending_progress.clear()


class Broker:
    """One LISTEN connection and the fan-out over it."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.subscribers: set[Subscriber] = set()
        self.buffer: deque[Message] = deque(maxlen=BUFFER_SIZE)
        #: The greatest event id dispatched. Everything above it is owed.
        self.watermark = 0
        self._tasks: list[asyncio.Task[None]] = []
        self._wake = asyncio.Event()
        self._stopping = False

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        async with await self._connect() as connection:
            row = await _one(connection, "select coalesce(max(id), 0) as id from catalogue.event_log")
            self.watermark = int(row["id"]) if row else 0

        self._tasks = [
            asyncio.create_task(self._listen(), name="broker-listen"),
            asyncio.create_task(self._reconcile(), name="broker-reconcile"),
            asyncio.create_task(self._roster(), name="broker-roster"),
            asyncio.create_task(self._coalesce(), name="broker-coalesce"),
        ]
        LOGGER.info("broker.started", watermark=self.watermark)

    async def stop(self) -> None:
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for subscriber in list(self.subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                subscriber.queue.put_nowait(None)
        LOGGER.info("broker.stopped")

    async def _connect(self) -> psycopg.AsyncConnection[dict[str, Any]]:
        return await psycopg.AsyncConnection.connect(
            self.settings.dsn, row_factory=dict_row, autocommit=True
        )

    # -- ingest -----------------------------------------------------------

    async def _listen(self) -> None:
        """Hold the LISTEN connection, and wake the dispatcher on any hint.

        The notification's payload is deliberately ignored beyond waking this
        up. It carries an id, but treating it as the thing to dispatch would
        make a dropped notify a lost event; querying above the watermark instead
        means the table is always the authority.
        """
        while not self._stopping:
            try:
                async with await self._connect() as connection:
                    await connection.execute("listen catalogue_ops")
                    await connection.execute("listen catalogue_progress")
                    # Re-establishing LISTEN always catches up first: anything
                    # that fired while disconnected is above the watermark.
                    await self._drain_events()
                    LOGGER.info("broker.listening")

                    async for notification in connection.notifies():
                        if self._stopping:
                            break
                        if notification.channel == "catalogue_progress":
                            await self._push_progress(notification.payload)
                        else:
                            await self._drain_events()
            except (psycopg.Error, OSError):
                if self._stopping:
                    return
                LOGGER.warning("broker.listen_failed", exc_info=True)
                await asyncio.sleep(2)

    async def _reconcile(self) -> None:
        """Poll above the watermark, whether or not a notification arrived.

        Without this, a `notify` lost while the listener was reconnecting is
        lost for ever if it happened to be the last event — and "the run
        finished but the UI still says running" is exactly the failure that
        makes people stop trusting a live view.
        """
        while not self._stopping:
            await asyncio.sleep(RECONCILE_SECONDS)
            with contextlib.suppress(psycopg.Error, OSError):
                await self._drain_events()

    async def _drain_events(self) -> None:
        async with await self._connect() as connection:
            rows = await _all(
                connection,
                "select id, at, topic, type, run_id, job_id, worker_id, source_id, payload "
                "from catalogue.event_log where id > %(after)s order by id limit 500",
                {"after": self.watermark},
            )
        for row in rows:
            self.watermark = max(self.watermark, int(row["id"]))
            self.publish(_event_message(row))

    async def _push_progress(self, job_id: str) -> None:
        """Read one job's current counters and publish them as a level."""
        async with await self._connect() as connection:
            row = await _one(
                connection,
                "select p.*, j.source_id, j.run_id from catalogue.job_progress p "
                "join catalogue.jobs j on j.id = p.job_id where p.job_id = %(id)s",
                {"id": job_id},
            )
        if row is None:
            return
        self.publish(
            Message(
                event="job.progress",
                topic="progress",
                # No id: a level is never replayed, and numbering it would put
                # it in the same sequence clients resume from.
                event_id=None,
                run_id=str(row["run_id"]),
                job_id=str(row["job_id"]),
                data={
                    "job_id": str(row["job_id"]),
                    "run_id": str(row["run_id"]),
                    "source": row["source_id"],
                    "phase": row["phase"],
                    "records": row["records"],
                    "requests": row["requests"],
                    "rendered_pages": row["rendered_pages"],
                    "errors": row["error_count"],
                    "discovered": row["discovered"],
                    "truncated": row["truncated"],
                    "in_flight": row["in_flight"],
                    "at": row["updated_at"],
                },
            )
        )

    async def _roster(self) -> None:
        """Push the worker roster as a level, every few seconds.

        This is how a silently dead worker becomes visible. No event fires when
        a process stops existing, so the roster carries `last_heartbeat_at` and
        the browser renders the age itself — going amber then red on its own.
        """
        while not self._stopping:
            await asyncio.sleep(ROSTER_SECONDS)
            if not any("workers" in s.topics for s in self.subscribers):
                continue
            try:
                async with await self._connect() as connection:
                    workers = await _all(connection, WORKER_ROSTER)
            except (psycopg.Error, OSError):
                continue
            self.publish(
                Message(
                    event="worker.roster",
                    topic="workers",
                    event_id=None,
                    data={"workers": [_worker(row) for row in workers]},
                )
            )

    async def _coalesce(self) -> None:
        """Flush each subscriber's pending progress on a fixed window.

        An 80-source run emitting progress at 1 Hz is 80 messages a second to
        every browser, to render a table that changes visibly perhaps twice a
        second.
        """
        while not self._stopping:
            await asyncio.sleep(COALESCE_SECONDS)
            for subscriber in list(self.subscribers):
                subscriber.flush_progress()

    # -- fan-out ----------------------------------------------------------

    def publish(self, message: Message) -> None:
        if message.event_id is not None:
            self.buffer.append(message)
        for subscriber in list(self.subscribers):
            if subscriber.wants(message):
                subscriber.offer(message)

    async def replay(self, subscriber: Subscriber, last_event_id: int) -> None:
        """Send everything after `last_event_id`, from memory or from the table.

        The buffer is checked first because it is free. When the gap is older
        than the buffer the rows come from `catalogue.event_log`, which is the
        entire reason that table exists — and when the gap is larger than
        `MAX_REPLAY`, replaying thousands of rows serves nobody, so the client
        is told to refetch instead.
        """
        if self.watermark - last_event_id > MAX_REPLAY:
            subscriber.offer(
                Message(event="resync", topic="control", data={"reason": "gap too large"})
            )
            return

        buffered = [m for m in self.buffer if m.event_id is not None and m.event_id > last_event_id]
        oldest = min((m.event_id or 0) for m in self.buffer) if self.buffer else 0
        if buffered and oldest <= last_event_id + 1:
            for message in buffered:
                if subscriber.wants(message):
                    subscriber.offer(message)
            return

        try:
            async with await self._connect() as connection:
                rows = await _all(
                    connection,
                    "select id, at, topic, type, run_id, job_id, worker_id, source_id, payload "
                    "from catalogue.event_log where id > %(after)s order by id limit %(limit)s",
                    {"after": last_event_id, "limit": MAX_REPLAY},
                )
        except psycopg.Error:
            subscriber.offer(Message(event="resync", topic="control", data={"reason": "replay failed"}))
            return

        for row in rows:
            message = _event_message(row)
            if subscriber.wants(message):
                subscriber.offer(message)

    async def subscribe(
        self, topics: frozenset[str], run_id: str | None, last_event_id: int | None
    ) -> AsyncIterator[Subscriber]:
        subscriber = Subscriber(topics=topics, run_id=run_id)
        self.subscribers.add(subscriber)
        LOGGER.info("stream.subscribed", topics=sorted(topics), run_id=run_id,
                    subscribers=len(self.subscribers))
        try:
            if last_event_id is not None:
                await self.replay(subscriber, last_event_id)
            yield subscriber
        finally:
            self.subscribers.discard(subscriber)
            LOGGER.info("stream.unsubscribed", subscribers=len(self.subscribers))


WORKER_ROSTER = """
select w.id, w.hostname, w.pid, w.version, w.capabilities, w.status, w.desired_state,
       w.started_at, w.last_heartbeat_at, w.current_job_id,
       j.source_id as current_source,
       coalesce((
         select jsonb_agg(jsonb_build_object(
                  'job_id', active.id::text,
                  'run_id', active.run_id::text,
                  'source', active.source_id
                ) order by active.source_id)
           from catalogue.jobs active
          where active.lease_owner = w.id
            and active.state in ('leased', 'running', 'paused')
       ), '[]'::jsonb) as current_jobs
  from catalogue.workers w
  left join catalogue.jobs j on j.id = w.current_job_id
 where w.status <> 'stopped'
 order by w.started_at
"""


def _worker(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "worker_id": str(row["id"]),
        "hostname": row["hostname"],
        "pid": row["pid"],
        "version": row["version"],
        "capabilities": list(row["capabilities"] or []),
        "status": row["status"],
        "desired_state": row["desired_state"],
        "started_at": row["started_at"],
        # The browser derives the age from this locally, every second, so a
        # worker that died goes stale without any event arriving.
        "last_heartbeat_at": row["last_heartbeat_at"],
        "current_job_id": str(row["current_job_id"]) if row["current_job_id"] else None,
        "current_source": row["current_source"],
        "current_jobs": list(row["current_jobs"] or []),
    }


def _event_message(row: dict[str, Any]) -> Message:
    return Message(
        event=row["type"],
        topic=TOPIC_OF.get(row["topic"], row["topic"]),
        event_id=int(row["id"]),
        run_id=str(row["run_id"]) if row["run_id"] else None,
        job_id=str(row["job_id"]) if row["job_id"] else None,
        data={
            "id": int(row["id"]),
            "at": row["at"],
            "type": row["type"],
            "run_id": str(row["run_id"]) if row["run_id"] else None,
            "job_id": str(row["job_id"]) if row["job_id"] else None,
            "worker_id": str(row["worker_id"]) if row["worker_id"] else None,
            "source": row["source_id"],
            **(row["payload"] or {}),
        },
    )


def parse_topics(raw: str | None) -> frozenset[str]:
    if not raw:
        return frozenset(DEFAULT_TOPICS)
    wanted = {item.strip() for item in raw.split(",") if item.strip()}
    return frozenset(wanted & set(ALL_TOPICS)) or frozenset(DEFAULT_TOPICS)


async def _one(
    connection: psycopg.AsyncConnection[dict[str, Any]], sql: str, params: Any = None
) -> dict[str, Any] | None:
    async with connection.cursor() as cursor:
        await cursor.execute(sql, params)
        return await cursor.fetchone()


async def _all(
    connection: psycopg.AsyncConnection[dict[str, Any]], sql: str, params: Any = None
) -> list[dict[str, Any]]:
    async with connection.cursor() as cursor:
        await cursor.execute(sql, params)
        return await cursor.fetchall()
