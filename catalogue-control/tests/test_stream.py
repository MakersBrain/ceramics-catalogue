"""The live stream: bootstrap, edges, levels, replay and backpressure.

Almost every assertion here is about the edge/level split (§3.1), because it is
the load-bearing part of the design and it fails silently when broken. Progress
in the event log does not error — it just makes the log ~860,000 rows per run
and quietly destroys replay.

The broker is exercised directly where the behaviour is internal (coalescing,
queue bounds) and over HTTP where the wire format is the point.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from catalogue_control.broker import (
    MAX_REPLAY,
    Broker,
    Message,
    Subscriber,
    parse_topics,
)
from catalogue_control.settings import Settings

from .conftest import TOKEN, postgres_dsn, requires_postgres


class TestTopicSelection:
    """No database needed: this is pure request parsing."""

    def test_omitting_topics_subscribes_to_everything_except_progress(self):
        """Progress is the expensive one and should be asked for deliberately."""
        topics = parse_topics(None)
        assert "workers" in topics
        assert "notifications" in topics
        assert "progress" not in topics

    def test_unknown_topics_are_ignored_rather_than_erroring(self):
        assert parse_topics("workers,nonsense") == frozenset({"workers"})

    def test_an_entirely_unknown_selection_falls_back_to_the_default(self):
        assert "workers" in parse_topics("nonsense")


class TestSubscriberFiltering:
    def edge(self, topic="jobs", run_id=None, job_id=None, event_id=1):
        return Message(event="job.failed", data={}, topic=topic, event_id=event_id,
                       run_id=run_id, job_id=job_id)

    def test_a_subscriber_only_receives_its_topics(self):
        subscriber = Subscriber(topics=frozenset({"workers"}))
        assert not subscriber.wants(self.edge(topic="jobs"))
        assert subscriber.wants(self.edge(topic="workers"))

    def test_a_run_page_does_not_receive_other_runs_jobs(self):
        subscriber = Subscriber(topics=frozenset({"jobs"}), run_id="run-a")
        assert subscriber.wants(self.edge(run_id="run-a"))
        assert not subscriber.wants(self.edge(run_id="run-b"))

    def test_narrowing_by_run_does_not_filter_out_worker_events(self):
        """The layout still wants the roster while a run page is open."""
        subscriber = Subscriber(topics=frozenset({"jobs", "workers"}), run_id="run-a")
        assert subscriber.wants(self.edge(topic="workers", run_id=None))


class TestCoalescing:
    def test_progress_is_coalesced_per_job_with_the_latest_winning(self):
        """80 sources at 1 Hz is 80 messages a second, to render a table that
        changes visibly perhaps twice a second."""
        subscriber = Subscriber(topics=frozenset({"progress"}))
        for records in range(1, 50):
            subscriber.offer(
                Message(event="job.progress", topic="progress", job_id="job-1",
                        data={"records": records})
            )
        assert subscriber.queue.qsize() == 0, "progress was queued rather than coalesced"

        subscriber.flush_progress()
        assert subscriber.queue.qsize() == 1
        latest = subscriber.queue.get_nowait()
        assert latest is not None
        assert latest.data["records"] == 49

    def test_different_jobs_are_coalesced_separately(self):
        subscriber = Subscriber(topics=frozenset({"progress"}))
        for job in ("a", "b", "c"):
            subscriber.offer(
                Message(event="job.progress", topic="progress", job_id=job, data={})
            )
        subscriber.flush_progress()
        assert subscriber.queue.qsize() == 3


class TestBackpressure:
    def test_a_subscriber_that_falls_behind_is_told_to_resync(self):
        """Silently serving an incomplete history is the worse failure."""
        subscriber = Subscriber(topics=frozenset({"jobs"}))
        for index in range(1000):
            subscriber.offer(
                Message(event="job.failed", topic="jobs", event_id=index, data={})
            )
        assert subscriber.resync is True

    def test_progress_never_causes_an_overflow(self):
        """Levels are dropped by coalescing long before the queue fills."""
        subscriber = Subscriber(topics=frozenset({"progress"}))
        for index in range(10_000):
            subscriber.offer(
                Message(event="job.progress", topic="progress", job_id="one", data={"n": index})
            )
        assert subscriber.resync is False


class TestMessageEncoding:
    def test_an_edge_carries_an_id_so_it_can_be_replayed(self):
        wire = Message(event="job.failed", topic="jobs", event_id=42, data={"a": 1}).encode()
        assert wire.startswith("id: 42\n")
        assert "event: job.failed\n" in wire
        assert wire.endswith("\n\n")

    def test_a_level_carries_no_id(self):
        """`job.progress` is deliberately unnumbered: numbering it would put it
        in the sequence clients resume from, and replaying stale counters is
        worse than not replaying them."""
        wire = Message(event="job.progress", topic="progress", data={}).encode()
        assert "id:" not in wire
        assert wire.startswith("event: job.progress\n")


pytestmark_db = [pytest.mark.postgres, requires_postgres]


@pytest.mark.postgres
@requires_postgres
class TestAgainstTheDatabase:
    async def broker(self, db):
        broker = Broker(Settings(dsn=postgres_dsn() or "", control_token="x"))
        await broker.start()
        return broker

    async def test_the_watermark_starts_at_the_current_maximum(self, db):
        from ateliera_catalogue.ops import events

        await events.emit(db, events.Topic.WORKER, "worker.ready")
        broker = await self.broker(db)
        try:
            assert broker.watermark > 0
        finally:
            await broker.stop()

    async def test_an_edge_written_after_start_is_dispatched(self, db):
        from ateliera_catalogue.ops import events

        broker = await self.broker(db)
        subscriber = Subscriber(topics=frozenset({"workers"}))
        broker.subscribers.add(subscriber)
        try:
            await events.emit(db, events.Topic.WORKER, "worker.ready", payload={"n": 1})
            message = await asyncio.wait_for(subscriber.queue.get(), 5)
            assert message is not None
            assert message.event == "worker.ready"
            assert message.event_id is not None
        finally:
            await broker.stop()

    async def test_a_missed_notification_is_recovered_by_the_watermark(self, db):
        """The hole that makes LISTEN unusable as a queue on its own.

        Simulated by moving the watermark backwards, which is what a dropped
        `notify` looks like from the broker's side: rows exist above the last
        id it dispatched and nothing told it so. The reconciliation query is the
        only thing that closes it.
        """
        from ateliera_catalogue.ops import events

        broker = await self.broker(db)
        try:
            await events.emit(db, events.Topic.WORKER, "worker.ready")
            await asyncio.sleep(0.3)

            subscriber = Subscriber(topics=frozenset({"workers"}))
            broker.subscribers.add(subscriber)
            broker.watermark = 0  # pretend the notification never arrived

            await broker._drain_events()
            assert subscriber.queue.qsize() >= 1
        finally:
            await broker.stop()

    async def test_replay_reads_from_the_table_when_the_buffer_misses(self, db):
        """Which is the entire reason `catalogue.event_log` is durable."""
        from ateliera_catalogue.ops import events

        first = await events.emit(db, events.Topic.WORKER, "worker.ready")
        await events.emit(db, events.Topic.WORKER, "worker.changed")

        broker = await self.broker(db)
        try:
            broker.buffer.clear()  # nothing in memory to serve from
            subscriber = Subscriber(topics=frozenset({"workers"}))
            await broker.replay(subscriber, first)
            assert subscriber.queue.qsize() == 1
            replayed = subscriber.queue.get_nowait()
            assert replayed is not None
            assert replayed.event == "worker.changed"
        finally:
            await broker.stop()

    async def test_a_gap_larger_than_the_cap_asks_the_client_to_refetch(self, db):
        broker = await self.broker(db)
        try:
            broker.watermark = MAX_REPLAY + 100
            subscriber = Subscriber(topics=frozenset({"workers"}))
            await broker.replay(subscriber, 1)
            told = subscriber.queue.get_nowait()
            assert told is not None
            assert told.event == "resync"
        finally:
            await broker.stop()

    async def test_progress_is_never_written_to_the_event_log(self, db):
        """The invariant the whole stream design rests on."""
        from ateliera_catalogue.config.sources import SourcesFile
        from ateliera_catalogue.ops import runs
        from ateliera_catalogue.ops.sink import PostgresSink

        sources = SourcesFile.model_validate(
            {"ceradel": {"label": "C", "url": "https://ceradel.fr/", "scraper": "shopify"}}
        )
        run_id = await runs.create_run(db)
        assert run_id is not None
        jobs = await runs.create_jobs(db, run_id, sources, ["ceradel"])
        sink = PostgresSink(db, run_id, jobs, throttle=0)

        class Result:
            """The handful of attributes a sink reads off a live ScrapeResult."""

            def __init__(self) -> None:
                self.records = [{"n": 1}]
                self.requests = 1
                self.errors: list = []
                self.rendered_pages = 0
                self.discovered = 1
                self.truncated = False

        for _ in range(20):
            await sink.progress("ceradel", Result())

        async with db.cursor() as cursor:
            await cursor.execute("select type from catalogue.event_log")
            types = [row["type"] for row in await cursor.fetchall()]
        assert not any("progress" in name for name in types)


async def drive_stream(app, path: str, token: str, *, frames: int = 1, timeout: float = 10.0):
    """Drive the ASGI app directly and collect the first `frames` SSE frames.

    Not httpx: its `ASGITransport` collects the entire response body into a list
    before returning it (see `_transports/asgi.py`), so an endpoint that stays
    open for hours by design never returns at all. Every JSON route can be
    tested through httpx; a stream cannot, and pretending otherwise just hangs
    the suite.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": path.split("?")[0],
        "raw_path": path.encode(),
        "query_string": (path.split("?", 1)[1] if "?" in path else "").encode(),
        "headers": [(b"authorization", f"Bearer {token}".encode())],
        "client": ("test", 1),
        "server": ("control", 80),
        "scheme": "http",
        "root_path": "",
    }

    start: dict = {}
    body = ""
    collected: list[str] = []
    done = asyncio.Event()

    async def receive():
        # The client never sends anything and never disconnects until we cancel.
        await done.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        nonlocal body
        if message["type"] == "http.response.start":
            start.update(message)
        elif message["type"] == "http.response.body":
            body += message.get("body", b"").decode()
            while "\n\n" in body:
                frame, body = body.split("\n\n", 1)
                collected.append(frame)
                if len(collected) >= frames:
                    done.set()

    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(done.wait(), timeout)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    return start, collected


@pytest.mark.postgres
@requires_postgres
class TestStreamOverHttp:
    def build(self):
        from catalogue_control.app import create_app
        from catalogue_control.settings import Settings

        return create_app(Settings(dsn=postgres_dsn() or "", control_token=TOKEN))

    async def test_the_stream_opens_with_a_bootstrap_frame(self, db):
        """So a client never has to make a second request to render its first
        frame, and has nothing to reconcile against events that arrived in
        between."""
        app = self.build()
        async with app.router.lifespan_context(app):
            start, frames = await drive_stream(app, "/v1/events?topics=workers", TOKEN)

        assert start["status"] == 200
        headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
        assert headers["content-type"].startswith("text/event-stream")
        # Without this, nginx and Caddy buffer the stream into blocks and it
        # arrives in bursts or not at all.
        assert headers["x-accel-buffering"] == "no"

        assert frames[0].startswith("event: bootstrap")
        payload = json.loads(frames[0].split("data: ", 1)[1])
        for key in ("workers", "notifications", "queue", "watermark", "active_runs"):
            assert key in payload, key

    async def test_an_edge_reaches_a_connected_client(self, db):
        """The end-to-end claim: something written to `event_log` arrives on the
        wire, with an id, without the client asking again."""
        from ateliera_catalogue.ops import events

        app = self.build()
        async with app.router.lifespan_context(app):
            emitter = asyncio.create_task(_emit_after(db, 0.4))
            _, frames = await drive_stream(app, "/v1/events?topics=workers", TOKEN, frames=2)
            await emitter

        assert frames[0].startswith("event: bootstrap")
        assert "event: worker.ready" in frames[1]
        assert frames[1].startswith("id: "), "an edge must be numbered so it can be replayed"
        del events


async def _emit_after(connection, delay: float):
    from ateliera_catalogue.ops import events

    await asyncio.sleep(delay)
    await events.emit(connection, events.Topic.WORKER, "worker.ready", payload={"n": 1})
