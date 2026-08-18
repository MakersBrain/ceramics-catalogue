"""Publish the PostgreSQL transactional outbox to NATS JetStream."""

from __future__ import annotations

import asyncio

import psycopg

from mb_ceramics_catalogue.observability import logging as obs
from mb_ceramics_catalogue.observability import metrics
from mb_ceramics_catalogue.ops import outbox, queue
from mb_ceramics_catalogue.ops.job_queue import NatsJobQueue
from mb_ceramics_catalogue.storage.db import DictPool

LOGGER = obs.get_logger("catalogue.dispatcher")


class Dispatcher:
    def __init__(self, pool: DictPool, broker: NatsJobQueue) -> None:
        self.pool = pool
        self.broker = broker
        self.stopping = False
        self.ready = False

    async def run(self, *, once: bool = False) -> None:
        await self.broker.connect()
        self.ready = True
        while not self.stopping:
            published = await self.tick()
            if once:
                return
            if not published:
                await asyncio.sleep(1.0)

    async def tick(self) -> int:
        await self.broker.connect()
        published = 0
        async with self.pool.connection() as connection:
            try:
                async with connection.transaction():
                    await outbox.reconstruct_missing(connection)
                    await queue.reconcile(connection)
                    rows = await outbox.pending(connection)
                    for row in rows:
                        try:
                            await self.broker.publish(outbox.envelope(row))
                        except Exception as error:
                            await outbox.mark_failed(connection, row["id"], str(error))
                            LOGGER.warning(
                                "outbox.publish_failed",
                                job_id=str(row["job_id"]),
                                generation=row["generation"],
                                exc_info=True,
                            )
                            metrics.REGISTRY.counter(
                                "catalogue_outbox_publish_failures_total",
                                "Transactional outbox publications that failed.",
                            )
                        else:
                            await outbox.mark_published(connection, row["id"])
                            published += 1
                            metrics.REGISTRY.counter(
                                "catalogue_outbox_published_total",
                                "Transactional outbox rows confirmed by JetStream.",
                            )
            except psycopg.Error:
                LOGGER.warning("outbox.database_failed", exc_info=True)
        try:
            await self._observe()
        except Exception:
            LOGGER.warning("outbox.metrics_failed", exc_info=True)
        return published

    async def reconstruct(self) -> int:
        """Explicit disaster recovery after JetStream storage is replaced."""
        async with self.pool.connection() as connection, connection.transaction():
            count = await outbox.republish_current(connection)
            metrics.REGISTRY.counter(
                "catalogue_queue_reconstruction_publications_total",
                "Current job generations explicitly queued after broker-state loss.",
                amount=count,
            )
            return count

    async def _observe(self) -> None:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                """select count(*) filter (where published_at is null and cancelled_at is null) pending,
                          count(*) filter (where last_error is not null and published_at is null) failed,
                          coalesce(extract(epoch from now() - min(created_at) filter
                            (where published_at is null and cancelled_at is null)), 0) oldest
                     from catalogue.queue_outbox"""
            )
            row = await cursor.fetchone()
        assert row is not None
        for name in ("pending", "failed"):
            metrics.REGISTRY.gauge(
                f"catalogue_outbox_{name}", f"Outbox rows currently {name}.", float(row[name])
            )
        metrics.REGISTRY.gauge(
            "catalogue_outbox_oldest_seconds",
            "Age of the oldest unpublished outbox row.",
            float(row["oldest"]),
        )
        for route, values in (await self.broker.stats()).items():
            for state, value in values.items():
                metrics.REGISTRY.gauge(
                    "catalogue_queue_messages",
                    "JetStream messages by route and delivery state.",
                    value,
                    route=route,
                    state=state,
                )

    def describe(self) -> dict[str, str]:
        if self.stopping:
            return {"status": "stopping"}
        return {"status": "ready" if self.ready else "starting"}

    async def close(self) -> None:
        self.stopping = True
        await self.broker.close()
