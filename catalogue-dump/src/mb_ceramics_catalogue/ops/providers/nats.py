"""NATS JetStream adapters behind the provider-neutral queue contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any, cast

import nats
from nats.js.errors import NotFoundError

from mb_ceramics_catalogue.ops.delivery import (
    ROUTES,
    Delivery,
    JobEnvelope,
    Measurement,
    PublishReceipt,
    QueueRouteSnapshot,
    QueueSnapshot,
)
from mb_ceramics_catalogue.ops.job_queue import (
    STREAM,
    NatsJobQueue,
    durable_for,
)


class NatsPublisher:
    provider = "nats"

    def __init__(self, url: str, *, token: str = "", stream: str = STREAM, provision: bool = False) -> None:
        self.queue = NatsJobQueue(url, token=token, stream=stream, provision_on_connect=provision)

    async def connect(self) -> None:
        await self.queue.connect()

    async def publish(self, envelope: JobEnvelope) -> PublishReceipt:
        return await self.queue.publish(envelope)

    async def close(self) -> None:
        await self.queue.close()


class NatsConsumer:
    provider = "nats"

    def __init__(self, url: str, *, token: str = "", stream: str = STREAM, provision: bool = False) -> None:
        self.queue = NatsJobQueue(url, token=token, stream=stream, provision_on_connect=provision)

    async def connect(self) -> None:
        await self.queue.connect()

    async def next_delivery(self, routes: Sequence[str]) -> Delivery | None:
        return cast(Delivery | None, await self.queue.next_delivery(routes))

    def deliveries(self, routes: Sequence[str]) -> AsyncIterator[Delivery]:
        return cast(AsyncIterator[Delivery], self.queue.deliveries(routes))

    async def close(self) -> None:
        await self.queue.close()


class NatsProvisioner:
    provider = "nats"

    def __init__(
        self,
        url: str,
        *,
        token: str = "",
        stream: str = STREAM,
        subject_prefix: str = "catalogue.jobs",
    ) -> None:
        self.queue = NatsJobQueue(
            url,
            token=token,
            stream=stream,
            subject_prefix=subject_prefix,
            provision_on_connect=False,
        )

    async def apply(self, routes: Sequence[str] = ROUTES) -> None:
        if tuple(routes) != ROUTES:
            raise ValueError("NATS provisioning requires the complete route registry")
        await self.queue.connect()
        await self.queue.provision()

    async def validate(self, routes: Sequence[str] = ROUTES) -> list[str]:
        await self.queue.connect()
        js = self.queue._require_js()
        try:
            await js.stream_info(self.queue.stream)
        except NotFoundError:
            return [f"missing stream {self.queue.stream}"]
        issues: list[str] = []
        for route in routes:
            try:
                info = await js.consumer_info(self.queue.stream, durable_for(route))
            except NotFoundError:
                issues.append(f"missing route {route}")
                continue
            if info.config.filter_subject != self.queue.subject(route):
                issues.append(f"route {route} has an incorrect subject filter")
        return issues

    async def purge(self, routes: Sequence[str] = ROUTES) -> None:
        if tuple(routes) != ROUTES:
            raise ValueError("NATS purge requires the complete route registry")
        await self.queue.connect()
        js = self.queue._require_js()
        await js.purge_stream(self.queue.stream)

    async def close(self) -> None:
        await self.queue.close()


class NatsStatsReader:
    provider = "nats"

    def __init__(self, url: str, *, token: str = "", stream: str = STREAM) -> None:
        self.url = url
        self.token = token
        self.stream = stream
        self._last_success_at: datetime | None = None

    async def snapshot(self) -> QueueSnapshot:
        observed = datetime.now(UTC)
        options: dict[str, Any] = {
            "servers": [self.url],
            "name": "catalogue-queue-stats",
            "connect_timeout": 1,
            "allow_reconnect": False,
        }
        if self.token:
            options["token"] = self.token
        client = None
        try:
            client = await nats.connect(**options)
            js = client.jetstream()
            stream = await js.stream_info(self.stream)
            routes: list[QueueRouteSnapshot] = []
            delivered = 0
            for route in ROUTES:
                info = await js.consumer_info(self.stream, durable_for(route))
                delivered += int(info.delivered.consumer_seq or 0)
                routes.append(
                    QueueRouteSnapshot(
                        route=route,
                        ready=Measurement.exact(int(info.num_pending or 0)),
                        in_flight=Measurement.exact(int(info.num_ack_pending or 0)),
                        redelivered=Measurement.exact(int(info.num_redelivered or 0)),
                        delivered=Measurement.exact(int(info.delivered.consumer_seq or 0)),
                        oldest_age_seconds=Measurement.unsupported(),
                    )
                )
            self._last_success_at = observed
            return QueueSnapshot(
                provider=self.provider,
                observed_at=observed,
                last_success_at=observed,
                available=True,
                backlog_messages=Measurement.exact(int(stream.state.messages or 0)),
                backlog_bytes=Measurement.exact(int(stream.state.bytes or 0)),
                consumer_count=Measurement.exact(int(stream.state.consumer_count or 0)),
                routes=tuple(routes),
            )
        except Exception as error:  # noqa: BLE001 - provider failure is snapshot state
            return QueueSnapshot(
                provider=self.provider,
                observed_at=observed,
                last_success_at=self._last_success_at,
                available=False,
                backlog_messages=Measurement.unsupported(),
                backlog_bytes=Measurement.unsupported(),
                consumer_count=Measurement.unsupported(),
                routes=(),
                error=f"{type(error).__name__}: {str(error)[:160]}",
            )
        finally:
            if client is not None:
                await client.close()

    async def close(self) -> None:
        return None
