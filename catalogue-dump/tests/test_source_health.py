"""Taking a source out of runs while its supplier is down, and putting it back.

spectrum's WordPress lost its database on 2026-08-16. Retrying it is pointless
and scraping the WP Rocket copy still on disk is worse than pointless — it would
load 2026-08-14 prices as today's. So the source comes out; the probe is what
puts it back without anyone having to remember.
"""

from __future__ import annotations

import json

import pytest

from mb_ceramics_catalogue.config.sources import SourcesFile
from mb_ceramics_catalogue.ops import health, runs

from .conftest import requires_postgres

pytestmark = [pytest.mark.postgres, requires_postgres]

SOURCES = SourcesFile.model_validate(
    {
        "spectrum": {"label": "Spectrum", "url": "https://spectrum.test/", "scraper": "woocommerce"},
        "healthy": {"label": "Healthy", "url": "https://healthy.test/", "scraper": "shopify"},
    }
)
PROBE_URL = "https://spectrum.test/wp-json/wc/store/v1/products"


class FakeResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class FakeClient:
    """Answers each GET from a scripted list, and records what was asked."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []

    async def get(self, url: str):
        self.asked.append(url)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def down() -> FakeResponse:
    return FakeResponse(500, "<html><title>Database Error</title></html>")


def up() -> FakeResponse:
    return FakeResponse(200, json.dumps([{"id": 1, "name": "1100 TRANSPARENT"}]))


async def disable_spectrum(db, *, required_ok: int = 2) -> None:
    await health.disable(
        db, "spectrum", url=PROBE_URL,
        reason="WordPress cannot reach its database; every live route answers 500.",
        required_ok=required_ok,
    )


async def one(db, sql, params=None):
    cursor = await db.execute(sql, params)
    return await cursor.fetchone()


async def test_disabling_keeps_the_source_out_of_new_runs(db):
    """Disabled, not paused: a paused source's job never leaves `queued`."""
    await disable_spectrum(db)

    run_id = await runs.create_run(db)
    created = await runs.create_jobs(db, run_id, SOURCES, ["spectrum", "healthy"])

    assert "spectrum" not in created
    assert "healthy" in created


async def test_a_disabled_run_still_closes(db):
    """The reason `enabled` is the lever and `paused` is not.

    A paused source has a job created that `reserve` acknowledges without
    moving out of `queued`, so `_close_run_if_done` never sees a terminal set
    and the run hangs open for ever.
    """
    await disable_spectrum(db)
    run_id = await runs.create_run(db)
    created = await runs.create_jobs(db, run_id, SOURCES, ["spectrum", "healthy"])
    await runs.finish_job(db, created["healthy"], state="succeeded", summary={"records": 5})

    row = await one(db, "select status, finished_at from catalogue.runs where id = %s", (run_id,))
    assert row["status"] == "complete"
    assert row["finished_at"] is not None


async def test_disabling_records_how_to_tell_when_it_is_back(db):
    """A source disabled without a probe is one nobody re-enables."""
    await disable_spectrum(db)

    row = await one(db, "select * from catalogue.source_health_probes where source_id = 'spectrum'")
    assert row["url"] == PROBE_URL
    assert row["recovered_at"] is None
    assert "database" in row["reason"].lower()


async def test_a_site_still_down_stays_out(db):
    await disable_spectrum(db)
    client = FakeClient(down())

    assert await health.check_recovered(db, client=client) == 0

    row = await one(db, "select * from catalogue.source_health_probes where source_id = 'spectrum'")
    assert row["last_status"] == 500
    assert row["consecutive_ok"] == 0
    assert row["recovered_at"] is None
    settings = await one(db, "select enabled from catalogue.source_settings where source_id = 'spectrum'")
    assert settings["enabled"] is False


async def test_one_good_answer_is_not_enough(db):
    """Answering once proves the request went through, not that the site is back."""
    await disable_spectrum(db, required_ok=2)

    assert await health.check_recovered(db, client=FakeClient(up())) == 0

    settings = await one(db, "select enabled from catalogue.source_settings where source_id = 'spectrum'")
    assert settings["enabled"] is False


async def test_a_site_that_holds_up_comes_back_into_runs(db):
    await disable_spectrum(db, required_ok=2)

    assert await health.check_recovered(db, client=FakeClient(up())) == 0
    assert await health.check_recovered(db, client=FakeClient(up())) == 1

    settings = await one(db, "select enabled from catalogue.source_settings where source_id = 'spectrum'")
    assert settings["enabled"] is True
    row = await one(db, "select * from catalogue.source_health_probes where source_id = 'spectrum'")
    assert row["recovered_at"] is not None

    # And it is in the next run again, which is the whole point.
    run_id = await runs.create_run(db)
    created = await runs.create_jobs(db, run_id, SOURCES, ["spectrum", "healthy"])
    assert "spectrum" in created


async def test_a_flapping_site_does_not_accumulate_luck(db):
    """Any failure resets the count, so recovery has to be consecutive."""
    await disable_spectrum(db, required_ok=2)

    await health.check_recovered(db, client=FakeClient(up()))
    await health.check_recovered(db, client=FakeClient(down()))
    await health.check_recovered(db, client=FakeClient(up()))

    settings = await one(db, "select enabled from catalogue.source_settings where source_id = 'spectrum'")
    assert settings["enabled"] is False


async def test_a_cache_answering_200_with_an_error_page_is_not_recovery(db):
    """The exact failure being waited on: WP Rocket served pages while the
    database was down. A 200 whose body will not parse is not the API back."""
    await disable_spectrum(db, required_ok=1)
    client = FakeClient(FakeResponse(200, "<html><title>Database Error</title></html>"))

    assert await health.check_recovered(db, client=client) == 0

    row = await one(db, "select * from catalogue.source_health_probes where source_id = 'spectrum'")
    assert row["last_status"] == 200
    assert "not JSON" in row["last_error"]


async def test_a_transport_failure_is_recorded_not_raised(db):
    """This runs inside the leader tick; an unreachable host must not stop it."""
    await disable_spectrum(db)
    client = FakeClient(ConnectionError("connection refused"))

    assert await health.check_recovered(db, client=client) == 0

    row = await one(db, "select * from catalogue.source_health_probes where source_id = 'spectrum'")
    assert row["last_status"] is None
    assert "connection refused" in row["last_error"]


async def test_a_recovered_source_is_not_probed_again(db):
    await disable_spectrum(db, required_ok=1)
    await health.check_recovered(db, client=FakeClient(up()))

    client = FakeClient()
    assert await health.check_recovered(db, client=client) == 0
    assert client.asked == []


async def test_recovery_clears_the_warning_that_announced_it(db):
    """An alert that never clears is one people learn to ignore."""
    await disable_spectrum(db, required_ok=1)
    open_before = await one(
        db,
        "select count(*) as open from catalogue.notifications "
        "where dedup_key = 'source.disabled:spectrum' and resolved_at is null",
    )
    assert open_before["open"] == 1

    await health.check_recovered(db, client=FakeClient(up()))

    open_after = await one(
        db,
        "select count(*) as open from catalogue.notifications "
        "where dedup_key = 'source.disabled:spectrum' and resolved_at is null",
    )
    assert open_after["open"] == 0


async def test_a_run_whose_every_source_is_disabled_still_closes(db):
    """No job is created, so no job's finish would ever close the run.

    An operator checking on a disabled source by running it alone is the
    likeliest way to reach this, and a run stuck in `queued` for ever is what
    choosing `enabled` over `paused` was meant to avoid.
    """
    await disable_spectrum(db)
    run_id = await runs.create_run(db)

    created = await runs.create_jobs(db, run_id, SOURCES, ["spectrum"])

    assert created == {}
    row = await one(db, "select status, finished_at from catalogue.runs where id = %s", (run_id,))
    assert row["status"] == "complete"
    assert row["finished_at"] is not None


async def test_disabling_clears_the_warnings_it_supersedes(db):
    """`source.stale` and `job.failed` both resolve on the next success.

    A source out of runs has no next success, so left alone they would sit open
    for as long as the supplier takes to fix their database.
    """
    from mb_ceramics_catalogue.ops import events

    await events.notify(
        db, "source.stale", "spectrum has returned no records for 3 runs",
        dedup_key="source.stale:spectrum", source_id="spectrum",
    )
    await events.notify(
        db, "job.failed", "spectrum failed on attempt 1 of 3",
        dedup_key="job.failed:spectrum", source_id="spectrum",
    )

    await disable_spectrum(db)

    row = await one(
        db,
        "select count(*) as open from catalogue.notifications "
        "where source_id = 'spectrum' and resolved_at is null",
    )
    # Only the one that says why it is out.
    assert row["open"] == 1
    kind = await one(
        db,
        "select kind from catalogue.notifications "
        "where source_id = 'spectrum' and resolved_at is null",
    )
    assert kind["kind"] == "source.disabled"
