"""Runs, jobs, edges and progress against a real PostgreSQL.

These need a database because what they are testing *is* the database: an
`on conflict do nothing` against a partial unique index, a `for update` that
serialises two finishers, a trigger that fires a notify. None of that can be
checked against a mock, and all of it is what the operations UI depends on.

Run them with `CATALOGUE_TEST_DSN` pointed at a throwaway PostgreSQL.
"""

from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID, uuid4

import pytest

from mb_ceramics_catalogue import scrapers
from mb_ceramics_catalogue.config.sources import SourcesFile
from mb_ceramics_catalogue.ops import events, leases, runs
from mb_ceramics_catalogue.ops.sink import JobLogHandler, PostgresSink
from mb_ceramics_catalogue.scrapers.activity import CURRENT_JOB
from mb_ceramics_catalogue.storage import db as storage_db

from .conftest import requires_postgres

pytestmark = [pytest.mark.postgres, requires_postgres]


SOURCES = SourcesFile.model_validate(
    {
        "les-cousins": {"label": "Les Cousins", "url": "https://lescousins.fr/", "scraper": "woocommerce"},
        "ceradel": {"label": "Ceradel", "url": "https://ceradel.fr/", "scraper": "shopify"},
        # Same host as les-cousins, to exercise the per-host stagger.
        "les-cousins-two": {"label": "LC2", "url": "https://lescousins.fr/other", "scraper": "woocommerce"},
        "ceramicolours": {"label": "Ceramicolours", "url": "https://www.ceramicolours.it/", "scraper": "ceramicolours"},
    }
)


class FakeResult:
    """Stands in for a live `ScrapeResult`, which is all a sink ever reads."""

    def __init__(self, records: int = 0, requests: int = 0, errors: int = 0) -> None:
        self.records = [{"n": index} for index in range(records)]
        self.requests = requests
        self.errors = [{"url": "x", "error": "y"}] * errors
        self.rendered_pages = 0
        self.discovered = records
        self.truncated = False


async def register_worker(connection) -> UUID:
    """A worker row, because `host_leases.leased_by` references one."""
    worker_id = uuid4()
    await connection.execute(
        "insert into catalogue.workers (id, hostname, pid, capabilities, status) "
        "values (%(id)s, 'test', 1, '{}', 'idle')",
        {"id": worker_id},
    )
    return worker_id


async def rows(connection, sql, params=None):
    async with connection.cursor() as cursor:
        await cursor.execute(sql, params)
        return await cursor.fetchall()


class TestRunsAndJobs:
    async def test_a_run_fans_out_to_one_job_per_source(self, db):
        run_id = await runs.create_run(db, kind="manual", requested_by="tests")
        jobs = await runs.create_jobs(db, run_id, SOURCES, ["les-cousins", "ceradel"])
        assert set(jobs) == {"les-cousins", "ceradel"}

        stored = {row["source_id"]: row for row in await rows(db, "select * from catalogue.jobs")}
        assert stored["ceradel"]["host"] == "ceradel.fr"
        assert stored["ceradel"]["state"] == "queued"
        assert stored["ceradel"]["attempt"] == 0

    async def test_browser_sources_declare_the_capability_they_need(self, db):
        """Only a worker started with `--capabilities browser` may claim these."""
        run_id = await runs.create_run(db)
        await runs.create_jobs(db, run_id, SOURCES, ["ceramicolours", "ceradel"])
        stored = {row["source_id"]: row for row in await rows(db, "select * from catalogue.jobs")}
        assert stored["ceramicolours"]["requires"] == ["browser"]
        assert stored["ceradel"]["requires"] == []

    async def test_two_sources_on_one_host_are_staggered(self, db):
        """Eighty jobs at 03:00:00 on eighty hosts is fine; two on one is not."""
        run_id = await runs.create_run(db)
        await runs.create_jobs(db, run_id, SOURCES, ["les-cousins", "les-cousins-two", "ceradel"])
        stored = {row["source_id"]: row for row in await rows(db, "select * from catalogue.jobs")}
        gap = stored["les-cousins-two"]["scheduled_for"] - stored["les-cousins"]["scheduled_for"]
        assert 25 <= gap.total_seconds() <= 35

        # A host with one source is not delayed at all. Compared as a tolerance
        # rather than an ordering: each insert evaluates its own `now()`, so the
        # row written last is a few milliseconds later without being staggered.
        undelayed = abs(
            (stored["ceradel"]["scheduled_for"] - stored["les-cousins"]["scheduled_for"]).total_seconds()
        )
        assert undelayed < 1

    async def test_a_disabled_source_gets_no_job(self, db):
        await db.execute(
            "insert into catalogue.source_settings (source_id, enabled) values ('ceradel', false)"
        )
        run_id = await runs.create_run(db)
        jobs = await runs.create_jobs(db, run_id, SOURCES, ["les-cousins", "ceradel"])
        assert set(jobs) == {"les-cousins"}

    async def test_a_paused_source_still_gets_a_job(self, db):
        """Paused means 'not right now', not 'not part of runs'.

        The job exists and simply will not be claimed, so resuming the source
        lets the work it already had proceed.
        """
        await db.execute(
            "insert into catalogue.source_settings (source_id, paused) values ('ceradel', true)"
        )
        run_id = await runs.create_run(db)
        jobs = await runs.create_jobs(db, run_id, SOURCES, ["ceradel"])
        assert set(jobs) == {"ceradel"}


class TestRunClosure:
    async def test_the_run_closes_when_its_last_job_finishes(self, db):
        run_id = await runs.create_run(db)
        jobs = await runs.create_jobs(db, run_id, SOURCES, ["les-cousins", "ceradel"])
        await runs.start_run(db, run_id)

        first = await runs.finish_job(db, jobs["les-cousins"], state="succeeded",
                                      summary={"records": 10, "requests": 3})
        assert first is None, "the run must not close while a sibling is outstanding"

        last = await runs.finish_job(db, jobs["ceradel"], state="succeeded",
                                     summary={"records": 5, "requests": 2})
        assert last is not None
        assert last["status"] == "complete"
        assert last["records"] == 15

    async def test_a_run_with_one_failure_is_degraded_not_failed(self, db):
        """79 of 80 catalogues collected is not a failed run.

        Calling it failed is how an alert stops being believed.
        """
        run_id = await runs.create_run(db)
        jobs = await runs.create_jobs(db, run_id, SOURCES, ["les-cousins", "ceradel"])
        await runs.finish_job(db, jobs["les-cousins"], state="succeeded", summary={"records": 10})
        result = await runs.finish_job(db, jobs["ceradel"], state="failed",
                                       summary={"records": 0}, error="the shop refused us")
        assert result["status"] == "degraded"

    async def test_a_run_where_everything_failed_is_failed(self, db):
        run_id = await runs.create_run(db)
        jobs = await runs.create_jobs(db, run_id, SOURCES, ["ceradel"])
        result = await runs.finish_job(db, jobs["ceradel"], state="failed", summary={"records": 0})
        assert result["status"] == "failed"

    async def test_closing_a_run_promotes_what_it_collected(self, db):
        """A loaded source is only half the point.

        Until promotion runs, "les-cousins sells PRAI" and "sio-2 sells PRAI"
        are unrelated rows rather than one product two shops price. Nothing
        called it, so the cross-supplier join was as old as the last time a
        person ran it by hand.
        """
        await db.execute(
            "insert into catalogue.manufacturers (id, name) values ('sio-2', 'SIO-2') "
            "on conflict do nothing"
        )
        await db.execute(
            "insert into catalogue.manufacturer_aliases (alias, manufacturer_id) "
            "values ('sio-2', 'sio-2') on conflict do nothing"
        )
        for source in ("les-cousins", "ceradel"):
            await db.execute(
                "insert into catalogue.sources (id, label) values (%(source)s, %(source)s) "
                "on conflict do nothing",
                {"source": source},
            )
            await db.execute(
                """
                insert into catalogue.source_products
                       (source_id, external_id, record_format, product_url, name,
                        brand, manufacturer_sku, active, first_seen_at, last_seen_at)
                values (%(source)s, %(source)s || ':prai', 'ceramics.catalogue_item.v2',
                        'https://example.test/' || %(source)s || '/prai',
                        'white stoneware', 'SiO-2', 'PRAI', true, now(), now())
                """,
                {"source": source},
            )

        run_id = await runs.create_run(db)
        jobs = await runs.create_jobs(db, run_id, SOURCES, ["les-cousins", "ceradel"])
        await runs.finish_job(db, jobs["les-cousins"], state="succeeded", summary={"records": 1})
        await runs.finish_job(db, jobs["ceradel"], state="succeeded", summary={"records": 1})

        cursor = await db.execute(
            """
            select c.sku_key, count(distinct sp.source_id) as shops
              from catalogue.canonical_products c
              join catalogue.source_products sp on sp.canonical_product_id = c.id
             where c.manufacturer_id = 'sio-2'
             group by 1
            """
        )
        row = await cursor.fetchone()
        assert row is not None, "the run closed without promoting anything"
        assert row["sku_key"] == "PRAI"
        assert row["shops"] == 2, "both shops must land on the one product"

    async def test_a_run_closes_even_when_promotion_cannot(self, db):
        """A database predating the promotion schema has no such function.

        The run's outcome is already committed by then and is not in question
        because a derived table could not be rebuilt.
        """
        await db.execute("drop function if exists catalogue.promote_canonical_products(text)")
        run_id = await runs.create_run(db)
        jobs = await runs.create_jobs(db, run_id, SOURCES, ["ceradel"])
        result = await runs.finish_job(db, jobs["ceradel"], state="succeeded",
                                       summary={"records": 1})
        assert result is not None
        assert result["status"] == "complete"

    async def test_concurrent_finishers_close_the_run_exactly_once(self, db):
        """Two workers finishing their last jobs at the same moment.

        Without the `for update` on the run row both see "no outstanding
        siblings", both compute a summary, and the recorded outcome is whichever
        committed second. The `run.complete` edge would also appear twice.
        """
        import psycopg
        from psycopg.rows import dict_row

        from .conftest import postgres_dsn

        run_id = await runs.create_run(db)
        jobs = await runs.create_jobs(db, run_id, SOURCES, ["les-cousins", "ceradel"])

        dsn = postgres_dsn()
        assert dsn is not None

        async def finish(source: str):
            async with await psycopg.AsyncConnection.connect(
                dsn, row_factory=dict_row, autocommit=True
            ) as connection:
                return await runs.finish_job(
                    connection, jobs[source], state="succeeded", summary={"records": 1}
                )

        results = await asyncio.gather(finish("les-cousins"), finish("ceradel"))
        closed = [result for result in results if result is not None]
        assert len(closed) == 1, "the run closed more than once"

        completions = await rows(
            db, "select id from catalogue.event_log where type like 'run.complete%'"
        )
        assert len(completions) == 1


class TestEdgesAndLevels:
    async def test_progress_never_reaches_the_event_log(self, db):
        """The load-bearing part of the SSE design (§3.1).

        If progress is ever written to `event_log` "for consistency", a
        three-hour run puts ~860,000 rows in it and replay stops working.
        """
        run_id = await runs.create_run(db)
        jobs = await runs.create_jobs(db, run_id, SOURCES, ["ceradel"])
        sink = PostgresSink(db, run_id, jobs, throttle=0)

        await sink.started("ceradel", FakeResult(), "shopify", "api_json")
        for count in range(1, 40):
            await sink.progress("ceradel", FakeResult(records=count, requests=count))

        logged = await rows(db, "select type from catalogue.event_log")
        assert not any("progress" in row["type"] for row in logged)

        current = await rows(db, "select * from catalogue.job_progress")
        assert len(current) == 1, "progress is one row per job, updated in place"
        assert current[0]["records"] == 39

    async def test_progress_writes_are_throttled(self, db):
        """3,000 requests must not become 3,000 writes."""
        run_id = await runs.create_run(db)
        jobs = await runs.create_jobs(db, run_id, SOURCES, ["ceradel"])
        sink = PostgresSink(db, run_id, jobs, throttle=60)

        await sink.started("ceradel", FakeResult(), "shopify", "api_json")
        for count in range(1, 100):
            await sink.progress("ceradel", FakeResult(records=count))

        current = await rows(db, "select records from catalogue.job_progress")
        # The `started` write went through; every subsequent one inside the
        # window was dropped. The counters are cumulative, so nothing is lost.
        assert current[0]["records"] == 0

    async def test_an_edge_is_ordered_by_one_sequence(self, db):
        first = await events.emit(db, events.Topic.WORKER, "worker.ready")
        second = await events.emit(db, events.Topic.JOB, "job.leased")
        assert second > first

    async def test_the_event_log_trigger_notifies_with_the_id_alone(self, db):
        """A payload carrying `in_flight` would exceed the 8000-byte notify cap
        and fail the insert; the id is a hint to go and read."""
        await db.execute("listen catalogue_ops")
        event_id = await events.emit(
            db, events.Topic.JOB, "job.failed", payload={"big": "x" * 9000}
        )
        received = [note.payload async for note in db.notifies(timeout=2, stop_after=1)]
        assert received == [str(event_id)]

    async def test_job_progress_notifies_on_its_own_channel(self, db):
        run_id = await runs.create_run(db)
        jobs = await runs.create_jobs(db, run_id, SOURCES, ["ceradel"])
        await db.execute("listen catalogue_progress")
        sink = PostgresSink(db, run_id, jobs, throttle=0)
        await sink.started("ceradel", FakeResult(), "shopify", "api_json")
        received = [note.payload async for note in db.notifies(timeout=2, stop_after=1)]
        assert received == [str(jobs["ceradel"])]


class TestNotifications:
    async def test_the_same_open_condition_is_raised_once(self, db):
        """Three retries of one source at 03:00 are one notification."""
        first = await events.notify(db, "job.failed", "ceradel failed", source_id="ceradel")
        second = await events.notify(db, "job.failed", "ceradel failed again", source_id="ceradel")
        assert first is not None
        assert second is None

    async def test_a_resolved_condition_may_recur(self, db):
        """A dedup key is a deduplicator, not a mute."""
        await events.notify(db, "source.stale", "ceradel is stale", source_id="ceradel")
        assert await events.resolve(db, "source.stale:ceradel", source_id="ceradel")
        again = await events.notify(db, "source.stale", "ceradel is stale", source_id="ceradel")
        assert again is not None

    async def test_acknowledging_is_idempotent(self, db):
        notification_id = await events.notify(db, "worker.lost", "worker gone",
                                              severity=events.Severity.CRITICAL)
        assert await events.acknowledge(db, notification_id, "rick")
        assert not await events.acknowledge(db, notification_id, "rick")

    async def test_raising_one_emits_an_edge(self, db):
        await events.notify(db, "host.blocking", "ceradel.fr is refusing us",
                            severity=events.Severity.CRITICAL)
        logged = await rows(db, "select type, payload from catalogue.event_log")
        assert any(row["type"] == "notification.raised" for row in logged)


def line(message: str, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord("catalogue", level, __file__, 1, message, None, None)


class TestJobLog:
    async def test_lines_reach_job_events(self, db):
        run_id = await runs.create_run(db)
        jobs = await runs.create_jobs(db, run_id, SOURCES, ["ceradel"])
        handler = JobLogHandler(jobs["ceradel"])

        CURRENT_JOB.set(str(jobs["ceradel"]))
        handler.emit(line("host=ceradel.fr failed (429)", logging.WARNING))
        assert await handler.flush_to(db) == 1

        stored = await rows(db, "select level, message from catalogue.job_events")
        assert stored[0]["level"] == "warning"
        assert "429" in stored[0]["message"]

    async def test_a_runaway_job_cannot_fill_the_queue(self, db):
        job_id = uuid4()
        handler = JobLogHandler(job_id, capacity=5)
        CURRENT_JOB.set(str(job_id))
        for index in range(50):
            handler.emit(line(f"line {index}"))
        drained = handler.drain()
        assert len(drained) == 6, "five lines plus one saying what was dropped"
        assert "dropped" in drained[-1][2]

    async def test_a_handler_takes_only_its_own_job_s_lines(self):
        """A worker with four job slots has four of these on the root logger.

        Every one of them was offered every record, so each job's log page
        showed all four jobs' lines — with the other jobs' ids inside the
        messages, which is at its most misleading exactly when someone is
        reading the page to find out why a job failed.
        """
        mine, theirs = uuid4(), uuid4()
        handler = JobLogHandler(mine)

        CURRENT_JOB.set(str(theirs))
        handler.emit(line("something the other job did"))
        assert handler.drain() == []

        CURRENT_JOB.set(str(mine))
        handler.emit(line("something this job did"))
        assert [entry[2] for entry in handler.drain()] == ["something this job did"]

    async def test_a_line_belonging_to_no_job_reaches_no_job_s_log(self):
        """The heartbeat and the queue are the worker's, not any one job's."""
        handler = JobLogHandler(uuid4())
        CURRENT_JOB.set("")
        handler.emit(line("worker.tick"))
        assert handler.drain() == []


class TestHostSlots:
    async def test_reconciling_creates_one_slot_per_unit_of_concurrency(self, db):
        await db.execute("insert into catalogue.hosts (host, max_concurrency) values ('ceradel.fr', 3)")
        await db.execute("select catalogue.reconcile_host_slots('ceradel.fr')")
        slots = await rows(db, "select slot from catalogue.host_leases order by slot")
        assert [row["slot"] for row in slots] == [1, 2, 3]

    async def test_an_unknown_host_gets_a_default_of_one(self, db):
        """One request at a time is the right default for a shop."""
        await db.execute("select catalogue.reconcile_host_slots('new-shop.test')")
        slots = await rows(db, "select slot from catalogue.host_leases where host='new-shop.test'")
        assert len(slots) == 1

    async def test_lowering_the_limit_never_takes_a_slot_from_a_running_job(self, db):
        """Taking the slot away does not stop the requests already in flight."""
        run_id = await runs.create_run(db)
        jobs = await runs.create_jobs(db, run_id, SOURCES, ["ceradel"])
        await db.execute("insert into catalogue.hosts (host, max_concurrency) values ('ceradel.fr', 3)")
        await db.execute("select catalogue.reconcile_host_slots('ceradel.fr')")
        await db.execute(
            "update catalogue.host_leases set job_id = %(job)s where host='ceradel.fr' and slot = 3",
            {"job": jobs["ceradel"]},
        )

        await db.execute("update catalogue.hosts set max_concurrency = 1 where host='ceradel.fr'")
        await db.execute("select catalogue.reconcile_host_slots('ceradel.fr')")

        remaining = await rows(
            db, "select slot, job_id from catalogue.host_leases where host='ceradel.fr' order by slot"
        )
        assert [row["slot"] for row in remaining] == [1, 3], "the occupied slot survived"

    async def test_two_shops_on_one_edge_cannot_run_at_once(self, db):
        """Politeness is per host, and a Shopify shop's host is not the whole story.

        Nineteen of these shops are Shopify storefronts on custom domains, all
        answering from one edge that meters by client address across every shop
        on it. Two of them crawled concurrently from one machine is the shape of
        the 2026-08-12 failure, and it looks perfectly polite per host.
        """
        run_id = await runs.create_run(db)
        jobs = await runs.create_jobs(db, run_id, SOURCES, ["ceradel", "les-cousins"])
        edge = scrapers.shared_edge("shopify")
        assert edge is not None

        first, second = await register_worker(db), await register_worker(db)
        assert await leases.acquire(db, "ceradel.fr", jobs["ceradel"], first) is not None
        assert await leases.acquire(db, edge, jobs["ceradel"], first) is not None

        # A different shop, its own host free, but the edge is taken.
        assert await leases.acquire(db, "lescousins.fr", jobs["les-cousins"], second) is not None
        assert await leases.acquire(db, edge, jobs["les-cousins"], second) is None

        # The first job going away frees both of its keys, whichever it holds.
        assert set(await leases.release_all(db, jobs["ceradel"])) == {"ceradel.fr", edge}
        assert await leases.acquire(db, edge, jobs["les-cousins"], second) is not None

    async def test_an_operator_can_widen_the_edge_without_a_deploy(self):
        """It is an ordinary row in `catalogue.hosts`, so it tunes like one."""
        assert scrapers.shared_edge("shopify") == "edge:shopify"
        assert scrapers.shared_edge("woocommerce") is None
        # `sio2` is a PrestaShop under another name: the class decides, not the key.
        assert scrapers.shared_edge("sio2") is None


class TestImportRunLink:
    async def test_a_load_can_be_traced_to_the_crawl_that_produced_it(self, db):
        run_id = await runs.create_run(db)
        await db.execute(
            "insert into catalogue.import_runs (status, importer_version, run_id) "
            "values ('complete', 'tests', %(run)s)",
            {"run": run_id},
        )
        found = await rows(
            db, "select run_id from catalogue.import_runs where run_id = %(run)s", {"run": run_id}
        )
        assert len(found) == 1


class TestScheduleDefault:
    async def test_the_daily_run_refreshes_rather_than_replaying(self, db):
        """A daily price run under the old seven-day cache default would replay
        yesterday's pages and report success while changing no prices."""
        found = await rows(db, "select cron, timezone, params from catalogue.schedules where id='daily-prices'")
        assert found[0]["cron"] == "0 3 * * *"
        assert found[0]["timezone"] == "Europe/Paris"
        params = found[0]["params"]
        if isinstance(params, str):
            params = json.loads(params)
        assert params["cache_mode"] == "refresh"
        assert params["refresh_mode"] == "price"
        weekly = await rows(
            db,
            "select cron, timezone, source_filter, params from catalogue.schedules "
            "where id='weekly-full'",
        )
        assert weekly[0]["cron"] == "0 2 * * 0"
        assert weekly[0]["params"]["refresh_mode"] == "full"


class TestSchemaMigration:
    async def test_an_existing_initdb_schema_is_adopted_then_migrations_are_recorded(self, db):
        first = await storage_db.apply_schema(db)
        assert "catalogue-reference-schema.sql" not in first
        assert "catalogue-reference-schema-v3.sql" in first
        assert await storage_db.apply_schema(db) == []
        applied = await rows(db, "select filename from catalogue.schema_migrations")
        assert {row["filename"] for row in applied} == set(storage_db.SCHEMA_FILES)
