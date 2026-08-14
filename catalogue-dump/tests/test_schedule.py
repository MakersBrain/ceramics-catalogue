"""Scheduling and the notification rules.

The scheduling tests are almost all about not doing something twice: firing an
occurrence twice, materialising a week of missed nights at once, or letting two
workers both decide they are the leader. The plan is explicit that "mutually
exclusive" is not enough, and these are the difference.

The monitor tests are about rules that must also *clear*. A `source.stale`
warning that stays up after the source recovers is how an alert feed becomes
wallpaper.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.rows import dict_row

from mb_ceramics_catalogue.config.sources import SourcesFile
from mb_ceramics_catalogue.ops import monitor, runs, schedule

from .conftest import postgres_dsn, requires_postgres

SOURCES = SourcesFile.model_validate(
    {
        "ceradel": {"label": "Ceradel", "url": "https://ceradel.fr/", "scraper": "shopify"},
        "spectrum": {"label": "Spectrum", "url": "https://www.spectrumglazes.com/", "scraper": "woocommerce"},
    }
)


class TestCronArithmetic:
    """No database needed."""

    def test_the_next_occurrence_is_in_the_schedules_own_timezone(self):
        """03:00 Europe/Paris is 01:00 or 02:00 UTC depending on the season.

        Doing this arithmetic in UTC silently moves the daily run by an hour for
        half the year, which nobody notices until a job runs twice on an October
        morning.
        """
        winter = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        summer = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

        assert schedule.next_fire("0 3 * * *", "Europe/Paris", winter).hour == 2
        assert schedule.next_fire("0 3 * * *", "Europe/Paris", summer).hour == 1

    def test_the_next_occurrence_is_strictly_after_the_reference(self):
        now = datetime(2026, 5, 1, 3, 0, tzinfo=UTC)
        assert schedule.next_fire("0 3 * * *", "Europe/Paris", now) > now

    def test_missed_occurrences_are_enumerated(self):
        start = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
        missed = schedule._missed_between("0 3 * * *", "Europe/Paris", start, end)
        assert len(missed) == 4


pytestmark = [pytest.mark.postgres, requires_postgres]


async def rows(connection, sql, params=None):
    async with connection.cursor() as cursor:
        await cursor.execute(sql, params)
        return await cursor.fetchall()


async def set_schedule(connection, **values):
    defaults = {
        "id": "daily-prices",
        "enabled": True,
        "cron": "* * * * *",
        "next_fire_at": datetime.now(UTC) - timedelta(minutes=1),
        "source_filter": '{"all": true}',
    }
    defaults.update(values)
    # Production seeds more than one schedule. Each firing test controls one
    # schedule explicitly, so keep unrelated defaults from becoming due based
    # on the wall clock and polluting its assertions.
    await connection.execute(
        "update catalogue.schedules set enabled = false where id <> %(id)s",
        {"id": defaults["id"]},
    )
    await connection.execute(
        "update catalogue.schedules set enabled = %(enabled)s, cron = %(cron)s, "
        "next_fire_at = %(next_fire_at)s, source_filter = %(source_filter)s::jsonb "
        "where id = %(id)s",
        defaults,
    )


class TestLeaderElection:
    async def test_a_leader_is_elected(self, db):
        async with db.transaction():
            assert await schedule.try_become_leader(db) is True

    async def test_only_one_connection_leads_at_a_time(self, db):
        """Two workers ticking at the same moment must not both fire the schedule."""
        dsn = postgres_dsn()
        assert dsn is not None

        held = asyncio.Event()
        release = asyncio.Event()

        async def hold():
            async with await psycopg.AsyncConnection.connect(
                dsn, row_factory=dict_row, autocommit=True
            ) as connection, connection.transaction():
                assert await schedule.try_become_leader(connection)
                held.set()
                await release.wait()

        holder = asyncio.create_task(hold())
        await asyncio.wait_for(held.wait(), 5)
        try:
            async with db.transaction():
                assert await schedule.try_become_leader(db) is False
        finally:
            release.set()
            await holder

    async def test_the_lock_is_released_when_the_transaction_ends(self, db):
        """Transaction-scoped, so it cannot leak on a pooled connection."""
        async with db.transaction():
            assert await schedule.try_become_leader(db)
        async with db.transaction():
            assert await schedule.try_become_leader(db)


class TestFiring:
    async def test_a_due_schedule_creates_a_run_and_its_jobs(self, db):
        await set_schedule(db)
        fired = await schedule.fire_due(db, SOURCES)

        assert len(fired) == 1
        assert fired[0]["jobs"] == 2
        run = (await rows(db, "select * from catalogue.runs"))[0]
        assert run["kind"] == "scheduled"
        assert run["schedule_id"] == "daily-prices"
        assert run["scheduled_fire_at"] is not None

    async def test_the_cursor_advances_so_it_does_not_fire_again(self, db):
        await set_schedule(db)
        await schedule.fire_due(db, SOURCES)
        assert await schedule.fire_due(db, SOURCES) == []
        assert len(await rows(db, "select id from catalogue.runs")) == 1

    async def test_the_same_occurrence_cannot_be_materialised_twice(self, db):
        """A leader that commits a run and dies before advancing the cursor.

        Mutual exclusion does nothing about this; the unique occurrence key is
        what makes the retry a no-op instead of a second run of eighty sources.
        """
        await set_schedule(db)
        fired = await schedule.fire_due(db, SOURCES)
        assert fired

        # Rewind the cursor: exactly what a crash after commit leaves behind.
        occurrence = (await rows(db, "select scheduled_fire_at from catalogue.runs"))[0]
        await db.execute(
            "update catalogue.schedules set next_fire_at = %(at)s where id = 'daily-prices'",
            {"at": occurrence["scheduled_fire_at"]},
        )

        assert await schedule.fire_due(db, SOURCES) == []
        assert len(await rows(db, "select id from catalogue.runs")) == 1

    async def test_a_disabled_schedule_does_not_fire(self, db):
        await set_schedule(db, enabled=False)
        assert await schedule.fire_due(db, SOURCES) == []

    async def test_a_long_outage_materialises_one_run_not_a_herd(self, db):
        """Eighty jobs per missed night, all starting at once, would hammer
        every shop in the catalogue simultaneously."""
        await set_schedule(db, cron="0 3 * * *", next_fire_at=datetime.now(UTC) - timedelta(days=10))
        fired = await schedule.fire_due(db, SOURCES)

        assert len(fired) == 1
        assert len(await rows(db, "select id from catalogue.runs")) == 1

    async def test_a_long_outage_raises_schedule_missed(self, db):
        await set_schedule(db, cron="0 3 * * *", next_fire_at=datetime.now(UTC) - timedelta(days=10))
        await schedule.fire_due(db, SOURCES)

        raised = await rows(db, "select kind, severity from catalogue.notifications")
        assert any(row["kind"] == "schedule.missed" for row in raised)
        assert all(row["severity"] == "critical" for row in raised)

    async def test_a_source_filter_narrows_the_run(self, db):
        await set_schedule(db, source_filter='{"only": ["ceradel"]}')
        fired = await schedule.fire_due(db, SOURCES)
        assert fired[0]["jobs"] == 1

    async def test_an_except_filter_removes_a_source(self, db):
        await set_schedule(db, source_filter='{"all": true, "except": ["ceradel"]}')
        fired = await schedule.fire_due(db, SOURCES)
        assert fired[0]["jobs"] == 1

    async def test_the_schedules_parameters_reach_the_run(self, db):
        """The daily run must refresh: replaying yesterday's pages would report
        success while changing no prices."""
        await set_schedule(db)
        await schedule.fire_due(db, SOURCES)
        run = (await rows(db, "select params from catalogue.runs"))[0]
        assert run["params"]["cache_mode"] == "refresh"


class TestNotificationRules:
    async def finished_job(self, connection, source, records, state="succeeded", ago=timedelta(0)):
        run_id = await runs.create_run(connection)
        assert run_id is not None
        jobs = await runs.create_jobs(connection, run_id, SOURCES, [source])
        await connection.execute(
            "update catalogue.jobs set state = %(state)s, finished_at = now() - %(ago)s, "
            "summary = %(summary)s::jsonb where id = %(id)s",
            {
                "id": jobs[source],
                "state": state,
                "ago": ago,
                "summary": f'{{"records": {records}}}',
            },
        )
        return jobs[source]

    async def test_three_empty_runs_make_a_source_stale(self, db):
        for index in range(3):
            await self.finished_job(db, "ceradel", 0, ago=timedelta(hours=index))
        assert await monitor.check_stale_sources(db) == 1

        raised = await rows(db, "select kind, source_id from catalogue.notifications")
        assert raised[0]["kind"] == "source.stale"
        assert raised[0]["source_id"] == "ceradel"

    async def test_two_empty_runs_do_not(self, db):
        """One empty run is a bad afternoon; three is a broken scraper."""
        for index in range(2):
            await self.finished_job(db, "ceradel", 0, ago=timedelta(hours=index))
        assert await monitor.check_stale_sources(db) == 0

    async def test_a_stale_source_that_recovers_clears_its_warning(self, db):
        """An alert that never clears is one people learn to ignore."""
        for index in range(3):
            await self.finished_job(db, "ceradel", 0, ago=timedelta(hours=index + 1))
        await monitor.check_stale_sources(db)

        await self.finished_job(db, "ceradel", 4000)
        await self.finished_job(db, "ceradel", 4000)
        await self.finished_job(db, "ceradel", 4000)
        await monitor.check_stale_sources(db)

        open_now = await rows(
            db, "select id from catalogue.notifications where resolved_at is null"
        )
        assert open_now == []

    async def test_a_source_that_halves_is_reported(self, db):
        """A scraper quietly returning half a catalogue reports success."""
        await self.finished_job(db, "ceradel", 4000, ago=timedelta(hours=2))
        await self.finished_job(db, "ceradel", 1200)
        assert await monitor.check_shrunk_sources(db) == 1

        raised = await rows(db, "select title from catalogue.notifications")
        assert "down 70%" in raised[0]["title"]

    async def test_a_small_source_losing_a_few_products_is_not_reported(self, db):
        """A 12-product source losing four is a 33% drop and means nothing."""
        await self.finished_job(db, "ceradel", 12, ago=timedelta(hours=2))
        await self.finished_job(db, "ceradel", 8)
        assert await monitor.check_shrunk_sources(db) == 0

    async def test_a_normal_days_variation_is_not_reported(self, db):
        await self.finished_job(db, "ceradel", 4000, ago=timedelta(hours=2))
        await self.finished_job(db, "ceradel", 3900)
        assert await monitor.check_shrunk_sources(db) == 0

    async def test_a_silent_worker_is_reported_as_lost(self, db):
        """Nothing fires when a process stops existing."""
        from uuid import uuid4

        await db.execute(
            "insert into catalogue.workers (id, hostname, pid, status, last_heartbeat_at) "
            "values (%(id)s, 'gone', 1, 'busy', now() - interval '5 minutes')",
            {"id": uuid4()},
        )
        assert await monitor.check_lost_workers(db) == 1
        raised = await rows(db, "select kind, severity from catalogue.notifications")
        assert raised[0]["kind"] == "worker.lost"
        assert raised[0]["severity"] == "critical"

    async def test_a_healthy_worker_is_not(self, db):
        from uuid import uuid4

        await db.execute(
            "insert into catalogue.workers (id, hostname, pid, status) values (%(id)s, 'fine', 1, 'idle')",
            {"id": uuid4()},
        )
        assert await monitor.check_lost_workers(db) == 0

    async def test_the_same_condition_is_reported_once(self, db):
        for index in range(3):
            await self.finished_job(db, "ceradel", 0, ago=timedelta(hours=index))
        assert await monitor.check_stale_sources(db) == 1
        assert await monitor.check_stale_sources(db) == 0


class TestRetention:
    async def test_old_log_lines_are_pruned(self, db):
        run_id = await runs.create_run(db)
        assert run_id is not None
        jobs = await runs.create_jobs(db, run_id, SOURCES, ["ceradel"])
        await db.execute(
            "insert into catalogue.job_events (job_id, at, level, message) "
            "values (%(job)s, now() - interval '60 days', 'info', 'ancient')",
            {"job": jobs["ceradel"]},
        )
        removed = await monitor.prune(db)
        assert removed["job_events"] == 1

    async def test_recent_log_lines_survive(self, db):
        run_id = await runs.create_run(db)
        assert run_id is not None
        jobs = await runs.create_jobs(db, run_id, SOURCES, ["ceradel"])
        await db.execute(
            "insert into catalogue.job_events (job_id, level, message) values (%(job)s, 'info', 'today')",
            {"job": jobs["ceradel"]},
        )
        await monitor.prune(db)
        assert len(await rows(db, "select id from catalogue.job_events")) == 1

    async def test_an_unresolved_notification_is_never_pruned(self, db):
        """It is the thing nobody dealt with. Deleting it turns "we never fixed
        that" into "we have no record of that"."""
        from mb_ceramics_catalogue.ops import events

        await events.notify(db, "source.stale", "old and unresolved", source_id="ceradel")
        await db.execute("update catalogue.notifications set at = now() - interval '200 days'")

        await monitor.prune(db)
        assert len(await rows(db, "select id from catalogue.notifications")) == 1

    async def test_a_resolved_notification_is_pruned_once_it_is_old(self, db):
        from mb_ceramics_catalogue.ops import events

        await events.notify(db, "source.stale", "dealt with", source_id="ceradel")
        await events.resolve(db, "source.stale:ceradel", source_id="ceradel")
        await db.execute("update catalogue.notifications set at = now() - interval '200 days'")

        removed = await monitor.prune(db)
        assert removed["notifications"] == 1
