"""The queue: claiming, capabilities, attempts, leases and host politeness.

Almost everything here is about a rule that is invisible until it is violated at
three in the morning:

* claiming must not consume an attempt, or host contention silently spends a
  source's retry budget;
* two workers must never hold the same job, or a shop is crawled twice at once;
* an expired lease must be recoverable, because a process that died says nothing;
* a job nothing can ever claim again must become terminal, or it stays `running`
  for ever with nothing running it;
* a host must never be crawled beyond its bound, whoever is asking.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from ateliera_catalogue.config.sources import SourcesFile
from ateliera_catalogue.ops import leases, queue, runs

from .conftest import postgres_dsn, requires_postgres

pytestmark = [pytest.mark.postgres, requires_postgres]

SOURCES = SourcesFile.model_validate(
    {
        "ceradel": {"label": "Ceradel", "url": "https://ceradel.fr/", "scraper": "shopify"},
        "spectrum": {"label": "Spectrum", "url": "https://www.spectrumglazes.com/", "scraper": "woocommerce"},
        "ceramicolours": {"label": "C", "url": "https://www.ceramicolours.it/", "scraper": "ceramicolours"},
        # Deliberately the same host as ceradel.
        "ceradel-two": {"label": "C2", "url": "https://ceradel.fr/other", "scraper": "shopify"},
    }
)


async def register_worker(connection, capabilities: list[str] | None = None):
    worker_id = uuid4()
    await connection.execute(
        "insert into catalogue.workers (id, hostname, pid, capabilities, status) "
        "values (%(id)s, 'test', 1, %(caps)s, 'idle')",
        {"id": worker_id, "caps": capabilities or []},
    )
    return worker_id


async def queued_run(connection, names: list[str], **kwargs):
    run_id = await runs.create_run(connection, **kwargs)
    assert run_id is not None
    jobs = await runs.create_jobs(connection, run_id, SOURCES, names)
    # `create_jobs` staggers same-host jobs into the future; these tests are
    # about claiming, not about scheduling, so bring them all due now.
    await connection.execute("update catalogue.jobs set scheduled_for = now()")
    return run_id, jobs


async def one(connection, sql, params=None):
    async with connection.cursor() as cursor:
        await cursor.execute(sql, params)
        return await cursor.fetchone()


class TestClaiming:
    async def test_a_queued_job_is_claimed(self, db):
        worker = await register_worker(db)
        await queued_run(db, ["ceradel"])

        job = await queue.claim(db, worker, [])
        assert job is not None
        assert job.source_id == "ceradel"
        assert job.host == "ceradel.fr"

    async def test_claiming_does_not_consume_an_attempt(self, db):
        """An attempt begins when the source runs, not when it is picked up.

        Host contention and a crash between claim and start are not scraper
        attempts, and counting them spends the retry budget on things the source
        never did.
        """
        worker = await register_worker(db)
        await queued_run(db, ["ceradel"])

        job = await queue.claim(db, worker, [])
        assert job.attempt == 0
        row = await one(db, "select attempt, state from catalogue.jobs")
        assert row["attempt"] == 0
        assert row["state"] == "leased"

    async def test_starting_consumes_the_attempt(self, db):
        worker = await register_worker(db)
        await queued_run(db, ["ceradel"])
        job = await queue.claim(db, worker, [])

        assert await queue.start(db, job, worker)
        row = await one(db, "select attempt, state from catalogue.jobs")
        assert row["attempt"] == 1
        assert row["state"] == "running"

    async def test_two_workers_never_hold_the_same_job(self, db):
        """`skip locked` is what makes this safe, and it is the whole reason
        Postgres can be the queue."""
        first = await register_worker(db)
        second = await register_worker(db)
        await queued_run(db, ["ceradel"])

        claimed_first = await queue.claim(db, first, [])
        claimed_second = await queue.claim(db, second, [])
        assert claimed_first is not None
        assert claimed_second is None

    async def test_concurrent_workers_take_different_jobs(self, db):
        dsn = postgres_dsn()
        await queued_run(db, ["ceradel", "spectrum"])
        workers = [await register_worker(db) for _ in range(4)]

        async def take(worker_id):
            async with await psycopg.AsyncConnection.connect(
                dsn, row_factory=dict_row, autocommit=True
            ) as connection:
                job = await queue.claim(connection, worker_id, [])
                return job.source_id if job else None

        taken = await asyncio.gather(*(take(worker) for worker in workers))
        claimed = sorted(name for name in taken if name)
        assert claimed == ["ceradel", "spectrum"], f"got {taken}"

    async def test_nothing_to_claim_returns_none(self, db):
        worker = await register_worker(db)
        assert await queue.claim(db, worker, []) is None

    async def test_a_job_scheduled_for_later_is_not_claimed_yet(self, db):
        worker = await register_worker(db)
        run_id = await runs.create_run(db)
        await runs.create_jobs(db, run_id, SOURCES, ["ceradel"])
        await db.execute("update catalogue.jobs set scheduled_for = now() + interval '1 hour'")
        assert await queue.claim(db, worker, []) is None


class TestCapabilities:
    async def test_a_plain_worker_cannot_take_a_browser_job(self, db):
        """camoufox makes the image large and the process memory-hungry, so most
        workers do not have one."""
        worker = await register_worker(db, [])
        await queued_run(db, ["ceramicolours"])
        assert await queue.claim(db, worker, []) is None

    async def test_a_browser_worker_can_take_a_browser_job(self, db):
        worker = await register_worker(db, ["browser"])
        await queued_run(db, ["ceramicolours"])
        job = await queue.claim(db, worker, ["browser"])
        assert job is not None
        assert job.source_id == "ceramicolours"

    async def test_a_browser_worker_can_still_take_plain_jobs(self, db):
        """Containment, not equality: a larger pool of capabilities is a
        superset, so a browser worker is not restricted to browser work."""
        worker = await register_worker(db, ["browser"])
        await queued_run(db, ["ceradel"])
        assert await queue.claim(db, worker, ["browser"]) is not None


class TestDiscoveredCapabilities:
    """A job can only be known to need a browser once a page has been read.

    `pagecrawl` retries a page through the browser when it parses to nothing,
    which depends on what the shop served that day — so no static list of
    browser sources can be complete, and a plain worker will sometimes find
    itself holding work it cannot do.
    """

    async def test_a_plain_worker_reroutes_rather_than_failing(self, db):
        plain = await register_worker(db, [])
        await queued_run(db, ["ceradel"])
        job = await queue.claim(db, plain, [])
        assert job is not None
        assert await queue.start(db, job, plain)

        assert await queue.require_capability(db, job, plain, "browser", reason="no camoufox")

        row = await db.execute("select state, requires from catalogue.jobs where id = %s", (job.id,))
        record = await row.fetchone()
        assert record["state"] == "queued"
        assert record["requires"] == ["browser"]

    async def test_the_rerouted_job_goes_to_a_browser_worker_and_not_back(self, db):
        plain = await register_worker(db, [])
        browser = await register_worker(db, ["browser"])
        await queued_run(db, ["ceradel"])
        job = await queue.claim(db, plain, [])
        await queue.start(db, job, plain)
        await queue.require_capability(db, job, plain, "browser", reason="no camoufox")

        # The worker that could not do it must not be offered it a second time.
        assert await queue.claim(db, plain, []) is None
        again = await queue.claim(db, browser, ["browser"])
        assert again is not None
        assert again.id == job.id

    async def test_rerouting_does_not_spend_the_sources_attempt(self, db):
        """The source did nothing wrong: the process was the wrong shape for it.

        Without this, three plain workers taking a browser job in turn would
        exhaust a source that was never actually crawled.
        """
        plain = await register_worker(db, [])
        browser = await register_worker(db, ["browser"])
        await queued_run(db, ["ceradel"])
        job = await queue.claim(db, plain, [])
        await queue.start(db, job, plain)
        assert job.attempt == 1

        await queue.require_capability(db, job, plain, "browser", reason="no camoufox")
        again = await queue.claim(db, browser, ["browser"])
        await queue.start(db, again, browser)
        assert again.attempt == 1

    async def test_a_job_that_already_requires_it_is_refused(self, db):
        """Otherwise a browser worker with a broken browser and a plain worker
        would bounce an impossible job between them for ever, neither of them
        ever spending an attempt, and nothing would ever report it as failed.

        The caller fails the job on a False.
        """
        browser = await register_worker(db, ["browser"])
        await queued_run(db, ["ceramicolours"])
        job = await queue.claim(db, browser, ["browser"])
        assert job.requires == ["browser"]
        await queue.start(db, job, browser)

        assert not await queue.require_capability(
            db, job, browser, "browser", reason="camoufox will not start"
        )
        row = await db.execute("select state from catalogue.jobs where id = %s", (job.id,))
        assert (await row.fetchone())["state"] == "running"

    async def test_rerouting_records_an_edge_for_the_operator(self, db):
        plain = await register_worker(db, [])
        await queued_run(db, ["ceradel"])
        job = await queue.claim(db, plain, [])
        await queue.start(db, job, plain)
        await queue.require_capability(db, job, plain, "browser", reason="no camoufox")

        row = await db.execute(
            "select type, payload from catalogue.event_log where job_id = %s "
            "and type = 'job.requeued'",
            (job.id,),
        )
        event = await row.fetchone()
        assert event is not None
        assert event["payload"]["requires"] == ["browser"]
        assert event["payload"]["reason"] == "no camoufox"


class TestSourceSettings:
    async def test_a_paused_source_is_not_claimed(self, db):
        worker = await register_worker(db)
        await queued_run(db, ["ceradel"])
        await db.execute(
            "insert into catalogue.source_settings (source_id, paused) values ('ceradel', true)"
        )
        assert await queue.claim(db, worker, []) is None

    async def test_resuming_a_source_makes_its_job_claimable_again(self, db):
        worker = await register_worker(db)
        await queued_run(db, ["ceradel"])
        await db.execute(
            "insert into catalogue.source_settings (source_id, paused) values ('ceradel', true)"
        )
        assert await queue.claim(db, worker, []) is None

        await db.execute("update catalogue.source_settings set paused = false")
        assert await queue.claim(db, worker, []) is not None

    async def test_a_cancelled_job_is_not_claimed(self, db):
        worker = await register_worker(db)
        await queued_run(db, ["ceradel"])
        await db.execute("update catalogue.jobs set cancel_requested = true")
        assert await queue.claim(db, worker, []) is None


class TestLeaseRecovery:
    async def test_an_expired_lease_is_reclaimed_by_another_worker(self, db):
        """A dead worker says nothing; the absence of a renewal is the signal."""
        dead = await register_worker(db)
        alive = await register_worker(db)
        await queued_run(db, ["ceradel"])

        job = await queue.claim(db, dead, [])
        await queue.start(db, job, dead)
        await db.execute("update catalogue.jobs set lease_expires_at = now() - interval '1 minute'")

        recovered = await queue.claim(db, alive, [])
        assert recovered is not None
        assert recovered.id == job.id

    async def test_reclaiming_after_a_crash_consumes_a_second_attempt(self, db):
        """The first attempt really was spent: the source was being crawled."""
        dead = await register_worker(db)
        alive = await register_worker(db)
        await queued_run(db, ["ceradel"])

        job = await queue.claim(db, dead, [])
        await queue.start(db, job, dead)
        await db.execute("update catalogue.jobs set lease_expires_at = now() - interval '1 minute'")

        recovered = await queue.claim(db, alive, [])
        await queue.start(db, recovered, alive)
        row = await one(db, "select attempt from catalogue.jobs")
        assert row["attempt"] == 2

    async def test_a_job_out_of_attempts_is_reaped_rather_than_left_running(self, db):
        """Otherwise it stays `running` for ever with no process running it."""
        dead = await register_worker(db)
        await queued_run(db, ["ceradel"])
        job = await queue.claim(db, dead, [])
        await queue.start(db, job, dead)
        await db.execute(
            "update catalogue.jobs set attempt = max_attempts, "
            "lease_expires_at = now() - interval '1 minute'"
        )

        reaped = await queue.reap_expired(db)
        assert len(reaped) == 1
        row = await one(db, "select state, error from catalogue.jobs")
        assert row["state"] == "failed"
        assert "lease expired" in row["error"]

    async def test_reaping_raises_one_notification(self, db):
        dead = await register_worker(db)
        await queued_run(db, ["ceradel"])
        job = await queue.claim(db, dead, [])
        await queue.start(db, job, dead)
        await db.execute(
            "update catalogue.jobs set attempt = max_attempts, "
            "lease_expires_at = now() - interval '1 minute'"
        )
        await queue.reap_expired(db)

        row = await one(db, "select kind, severity from catalogue.notifications")
        assert row["kind"] == "job.failed"

    async def test_a_job_below_its_limit_is_not_reaped(self, db):
        """The claim query picks expired leases up directly; a separate
        transition through `queued` would only add a state to reason about."""
        dead = await register_worker(db)
        await queued_run(db, ["ceradel"])
        job = await queue.claim(db, dead, [])
        await queue.start(db, job, dead)
        await db.execute("update catalogue.jobs set lease_expires_at = now() - interval '1 minute'")

        assert await queue.reap_expired(db) == []
        row = await one(db, "select state from catalogue.jobs")
        assert row["state"] == "running"

    async def test_starting_a_job_whose_lease_was_lost_is_refused(self, db):
        """Otherwise two workers crawl one shop at the same moment."""
        first = await register_worker(db)
        second = await register_worker(db)
        await queued_run(db, ["ceradel"])

        job = await queue.claim(db, first, [])
        await db.execute("update catalogue.jobs set lease_expires_at = now() - interval '1 minute'")
        stolen = await queue.claim(db, second, [])
        assert stolen is not None

        assert await queue.start(db, job, first) is False
        assert await queue.start(db, stolen, second) is True


class TestOperatorResume:
    async def test_a_resumed_job_does_not_spend_an_attempt(self, db):
        """A human pause must not exhaust the failure budget."""
        worker = await register_worker(db)
        await queued_run(db, ["ceradel"])
        job = await queue.claim(db, worker, [])
        await queue.start(db, job, worker)
        assert (await one(db, "select attempt from catalogue.jobs"))["attempt"] == 1

        await db.execute(
            "update catalogue.jobs set state='queued', lease_owner=null, lease_expires_at=null, "
            "resume_without_attempt = true"
        )
        resumed = await queue.claim(db, worker, [])
        await queue.start(db, resumed, worker)

        row = await one(db, "select attempt, resume_without_attempt from catalogue.jobs")
        assert row["attempt"] == 1, "an operator resume consumed an attempt"
        assert row["resume_without_attempt"] is False, "the flag must be cleared after use"

    async def test_a_job_out_of_attempts_is_claimable_when_flagged_for_resume(self, db):
        worker = await register_worker(db)
        await queued_run(db, ["ceradel"])
        await db.execute("update catalogue.jobs set attempt = max_attempts")
        assert await queue.claim(db, worker, []) is None

        await db.execute("update catalogue.jobs set resume_without_attempt = true")
        assert await queue.claim(db, worker, []) is not None


class TestRelease:
    async def test_releasing_returns_the_job_without_spending_an_attempt(self, db):
        worker = await register_worker(db)
        await queued_run(db, ["ceradel"])
        job = await queue.claim(db, worker, [])

        assert await queue.release(db, job, worker, delay=0)
        row = await one(db, "select state, attempt, lease_owner from catalogue.jobs")
        assert row["state"] == "queued"
        assert row["attempt"] == 0
        assert row["lease_owner"] is None

    async def test_a_released_job_waits_before_being_offered_again(self, db):
        worker = await register_worker(db)
        await queued_run(db, ["ceradel"])
        job = await queue.claim(db, worker, [])
        await queue.release(db, job, worker, delay=30)

        assert await queue.claim(db, worker, []) is None, "offered again immediately"


class TestHostPolitenessAcrossWorkers:
    async def test_one_slot_by_default_means_one_worker_at_a_time(self, db):
        """Three worker containers must not triple the load on every shop."""
        first = await register_worker(db)
        second = await register_worker(db)
        _, jobs = await queued_run(db, ["ceradel", "ceradel-two"])

        assert await leases.acquire(db, "ceradel.fr", jobs["ceradel"], first) == 1
        assert await leases.acquire(db, "ceradel.fr", jobs["ceradel-two"], second) is None

    async def test_a_released_slot_is_available_again(self, db):
        first = await register_worker(db)
        second = await register_worker(db)
        _, jobs = await queued_run(db, ["ceradel", "ceradel-two"])

        await leases.acquire(db, "ceradel.fr", jobs["ceradel"], first)
        await leases.release(db, "ceradel.fr", jobs["ceradel"])
        assert await leases.acquire(db, "ceradel.fr", jobs["ceradel-two"], second) == 1

    async def test_a_configured_host_allows_its_configured_concurrency(self, db):
        first = await register_worker(db)
        second = await register_worker(db)
        _, jobs = await queued_run(db, ["ceradel", "ceradel-two"])
        await leases.configure(db, "ceradel.fr", 2)

        assert await leases.acquire(db, "ceradel.fr", jobs["ceradel"], first) is not None
        assert await leases.acquire(db, "ceradel.fr", jobs["ceradel-two"], second) is not None

    async def test_an_expired_slot_is_taken_over(self, db):
        """A worker that died mid-request must not hold a shop for ever."""
        dead = await register_worker(db)
        alive = await register_worker(db)
        _, jobs = await queued_run(db, ["ceradel", "ceradel-two"])

        await leases.acquire(db, "ceradel.fr", jobs["ceradel"], dead)
        await db.execute("update catalogue.host_leases set leased_until = now() - interval '1 minute'")
        assert await leases.acquire(db, "ceradel.fr", jobs["ceradel-two"], alive) is not None

    async def test_concurrent_acquires_hand_out_each_slot_once(self, db):
        """Five workers, two slots. Exactly two may proceed.

        This is the assertion the whole `host_leases` table exists for: without
        it, five worker containers make five simultaneous requests to one shop
        and the shop starts refusing us.
        """
        dsn = postgres_dsn()
        _, jobs = await queued_run(db, ["ceradel", "ceradel-two"])
        await leases.configure(db, "ceradel.fr", 2)
        workers = [await register_worker(db) for _ in range(5)]
        # Real job ids: `host_leases.job_id` is a foreign key, so a fabricated
        # one fails the insert rather than testing contention.
        job_ids = list(jobs.values())

        async def take(worker_id, index):
            async with await psycopg.AsyncConnection.connect(
                dsn, row_factory=dict_row, autocommit=True
            ) as connection:
                return await leases.acquire(
                    connection, "ceradel.fr", job_ids[index % len(job_ids)], worker_id
                )

        got = await asyncio.gather(*(take(worker, i) for i, worker in enumerate(workers)))
        slots = sorted(slot for slot in got if slot is not None)
        assert slots == [1, 2], f"slots handed out: {got}"

    async def test_renewing_extends_only_this_workers_slots(self, db):
        first = await register_worker(db)
        second = await register_worker(db)
        _, jobs = await queued_run(db, ["ceradel", "spectrum"])
        await leases.acquire(db, "ceradel.fr", jobs["ceradel"], first)
        await leases.acquire(db, "www.spectrumglazes.com", jobs["spectrum"], second)

        assert await leases.renew(db, first) == 1


class TestQueueDepth:
    async def test_it_counts_only_unfinished_work(self, db):
        worker = await register_worker(db)
        _, jobs = await queued_run(db, ["ceradel", "spectrum"])
        job = await queue.claim(db, worker, [])
        await queue.start(db, job, worker)

        depth = await queue.queue_depth(db)
        assert depth == {"queued": 1, "running": 1}

        await runs.finish_job(db, jobs["ceradel"], state="succeeded", summary={"records": 1})
        assert await queue.queue_depth(db) == {"queued": 1}
