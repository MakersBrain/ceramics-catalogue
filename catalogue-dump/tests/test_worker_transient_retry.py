"""A source the host refused gets the attempts its job was granted.

`max_attempts` was reachable only through lease expiry — a worker that crashed
or hung. A source that ran to completion and collected nothing because the host
answered 429 finished normally, so it was recorded terminally on attempt 1 with
two attempts unspent. Every failure in the 2026-08-19 run was that shape.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from mb_ceramics_catalogue.config.settings import Settings
from mb_ceramics_catalogue.config.sources import SourcesFile
from mb_ceramics_catalogue.ops import events, leases, queue
from mb_ceramics_catalogue.ops.queue import ClaimedJob
from mb_ceramics_catalogue.ops.worker import Worker

pytestmark = pytest.mark.asyncio

THROTTLED = {
    "records": 0,
    "discovered": 0,
    "error_count": 1,
    "errors": [{"url": "https://shop.test/products.json", "error": "429 Too Many Requests"}],
    "outcome_counts": {"429": 9},
}


class Pool:
    @asynccontextmanager
    async def connection(self):
        yield object()


class StubDelivery:
    """Records which of the two mutually exclusive endings it was given."""

    def __init__(self) -> None:
        self.retried: list[float] = []
        self.acknowledged = 0

    async def retry(self, delay: float) -> None:
        self.retried.append(delay)

    async def acknowledge(self) -> None:
        self.acknowledged += 1


def make_job(attempt: int = 1, max_attempts: int = 3) -> ClaimedJob:
    return ClaimedJob(
        id=uuid4(),
        run_id=uuid4(),
        source_id="shop",
        host="shop.test",
        attempt=attempt,
        max_attempts=max_attempts,
        requires=[],
        requires_any=[],
        params={},
        proxy_snapshot={},
        delivery_generation=1,
        execution_token=uuid4(),
    )


class Harness:
    """The worker under test, with the three writes it makes recorded."""

    def __init__(self, worker):
        self.worker = worker
        self.released: list[dict] = []
        self.logged: list[tuple[str, str | None]] = []


@pytest.fixture
def harness(monkeypatch, tmp_path):
    sources = SourcesFile.model_validate(
        {"shop": {"label": "Shop", "url": "https://shop.test/", "scraper": "shopify"}}
    )
    built = Harness(Worker(Pool(), sources, Settings(dumps_dir=tmp_path)))

    async def release(connection, job, worker_id, *, delay=0, reason=""):
        built.released.append({"job": job.id, "delay": delay, "reason": reason})
        return True

    async def release_all(connection, job_id, execution_token):
        return []

    async def log(connection, job_id, message, **kwargs):
        built.logged.append((message, kwargs.get("event")))
        return 1

    monkeypatch.setattr(queue, "release", release)
    monkeypatch.setattr(leases, "release_all", release_all)
    monkeypatch.setattr(events, "log", log)
    return built


async def test_a_throttled_source_goes_back_to_the_queue(harness):
    job = make_job(attempt=1)
    delivery = StubDelivery()
    harness.worker._deliveries[job.id] = delivery

    assert await harness.worker._retry_transient(object(), job, THROTTLED) is True

    assert len(harness.released) == 1
    assert harness.released[0]["job"] == job.id


async def test_the_broker_is_told_to_redeliver_rather_than_to_drop_it(harness):
    """`release` only moves the row; redelivery has to come from the broker.

    Acknowledging here would move the job to `queued` and then throw away the
    message that would have started it, leaving it to sit until reconciliation.
    """
    job = make_job(attempt=1)
    delivery = StubDelivery()
    harness.worker._deliveries[job.id] = delivery

    await harness.worker._retry_transient(object(), job, THROTTLED)

    assert delivery.retried == [queue.TRANSIENT_BACKOFF_SECONDS]
    assert delivery.acknowledged == 0


async def test_the_delivery_is_forgotten_so_the_caller_cannot_ack_it(harness):
    """`_run_job`'s finally acknowledges whatever it still finds."""
    job = make_job(attempt=1)
    harness.worker._deliveries[job.id] = StubDelivery()

    await harness.worker._retry_transient(object(), job, THROTTLED)

    assert job.id not in harness.worker._deliveries


async def test_waiting_widens_with_each_attempt_already_spent(harness):
    """Still refused five minutes later is evidence the wait was short."""
    job = make_job(attempt=2)
    harness.worker._deliveries[job.id] = StubDelivery()

    await harness.worker._retry_transient(object(), job, THROTTLED)

    assert harness.released[0]["delay"] == 2 * queue.TRANSIENT_BACKOFF_SECONDS


async def test_the_last_attempt_is_left_to_fail(harness):
    """The budget being spent is the normal answer, not an error.

    The caller records the failure exactly as it did before, so a source whose
    host is down for good still lands in `failed` where an operator sees it.
    """
    job = make_job(attempt=3, max_attempts=3)
    delivery = StubDelivery()
    harness.worker._deliveries[job.id] = delivery

    assert await harness.worker._retry_transient(object(), job, THROTTLED) is False

    assert harness.released == []
    assert delivery.retried == []
    assert job.id in harness.worker._deliveries


async def test_a_job_whose_lease_moved_on_is_not_requeued(harness, monkeypatch):
    """Losing the row means this worker no longer speaks for the job."""
    async def refuse(connection, job, worker_id, *, delay=0, reason=""):
        return False

    monkeypatch.setattr(queue, "release", refuse)
    job = make_job(attempt=1)
    delivery = StubDelivery()
    harness.worker._deliveries[job.id] = delivery

    assert await harness.worker._retry_transient(object(), job, THROTTLED) is False

    assert delivery.retried == []
    assert job.id in harness.worker._deliveries


async def test_the_reason_the_host_gave_is_carried_into_the_log(harness):
    job = make_job(attempt=1)
    harness.worker._deliveries[job.id] = StubDelivery()

    await harness.worker._retry_transient(object(), job, THROTTLED)

    message, event = harness.logged[0]
    assert event == "job.retry_scheduled"
    assert "429 Too Many Requests" in message
    assert "attempt 1 of 3" in message


async def test_a_source_with_no_delivery_left_still_requeues_the_row(harness):
    """Shutdown can take the delivery first; the row must not stay running."""
    job = make_job(attempt=1)

    assert await harness.worker._retry_transient(object(), job, THROTTLED) is True

    assert len(harness.released) == 1
