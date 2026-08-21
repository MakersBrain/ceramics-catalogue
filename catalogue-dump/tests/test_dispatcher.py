from __future__ import annotations

import os

import pytest

from mb_ceramics_catalogue.config.sources import SourcesFile
from mb_ceramics_catalogue.ops import runs
from mb_ceramics_catalogue.ops.dispatcher import Dispatcher
from mb_ceramics_catalogue.ops.job_queue import NatsJobQueue
from mb_ceramics_catalogue.ops.providers.nats import NatsProvisioner
from mb_ceramics_catalogue.storage import db as storage_db

from .conftest import postgres_dsn, requires_postgres

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.nats,
    requires_postgres,
    pytest.mark.skipif(
        not os.environ.get("CATALOGUE_TEST_NATS_URL"),
        reason="set CATALOGUE_TEST_NATS_URL to a disposable JetStream server",
    ),
]


async def test_committed_outbox_is_published_and_marked(db) -> None:
    sources = SourcesFile.model_validate(
        {"shop": {"label": "Shop", "url": "https://shop.test/", "scraper": "shopify"}}
    )
    run_id = await runs.create_run(db)
    assert run_id is not None
    jobs = await runs.create_jobs(db, run_id, sources, ["shop"])
    await db.execute(
        "update catalogue.jobs set scheduled_for=now() where id=%(id)s", {"id": jobs["shop"]}
    )
    await db.execute(
        "update catalogue.queue_outbox set available_at=now() where job_id=%(id)s",
        {"id": jobs["shop"]},
    )

    provisioner = NatsProvisioner(
        os.environ["CATALOGUE_TEST_NATS_URL"],
        token=os.environ.get("CATALOGUE_TEST_NATS_TOKEN", ""),
    )
    await provisioner.apply()
    broker = NatsJobQueue(
        os.environ["CATALOGUE_TEST_NATS_URL"],
        token=os.environ.get("CATALOGUE_TEST_NATS_TOKEN", ""),
    )
    assert postgres_dsn() is not None
    async with storage_db.pool(postgres_dsn() or "", minimum=1, maximum=2) as pool:
        dispatcher = Dispatcher(pool, broker)
        try:
            assert await dispatcher.tick() == 1
            delivery = await broker.next_delivery(["plain.normal"])
            assert delivery is not None
            assert delivery.envelope.job_id == jobs["shop"]
            await delivery.ack()
            assert await dispatcher.reconstruct() == 1
            assert await dispatcher.tick() == 1
        finally:
            await dispatcher.close()
            await provisioner.close()

    cursor = await db.execute(
        "select published_at from catalogue.queue_outbox where job_id=%(id)s",
        {"id": jobs["shop"]},
    )
    assert (await cursor.fetchone())["published_at"] is not None
