"""NATS JetStream work delivery.

JetStream carries only a small, versioned reference. PostgreSQL remains the
authority for whether that reference is current and which execution owns the
job. Delivery is deliberately at least once; generation and execution-token
compare-and-set operations make duplicates harmless.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import nats
from nats.aio.client import Client as NATS
from nats.aio.msg import Msg
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.api import (
    AckPolicy,
    ConsumerConfig,
    DeliverPolicy,
    RetentionPolicy,
    StorageType,
    StreamConfig,
)
from nats.js.client import JetStreamContext
from nats.js.errors import NotFoundError

from mb_ceramics_catalogue.observability import logging as obs
from mb_ceramics_catalogue.observability import metrics
from mb_ceramics_catalogue.ops.delivery import (
    ROUTES,
    JobEnvelope,
    PublishReceipt,
)
from mb_ceramics_catalogue.ops.delivery import routes_for as routes_for

LOGGER = obs.get_logger("catalogue.nats")

STREAM = "CATALOGUE_JOBS"
SUBJECT_PREFIX = "catalogue.jobs.v1"
ACK_WAIT_SECONDS = 30.0


@dataclass
class Delivery:
    envelope: JobEnvelope
    message: Msg

    @property
    def provider_message_id(self) -> str | None:
        metadata = self.message.metadata
        return str(metadata.sequence.stream) if metadata is not None else None

    @property
    def delivery_attempt(self) -> int | None:
        metadata = self.message.metadata
        return int(metadata.num_delivered) if metadata is not None else None

    @property
    def remaining_delivery_attempts(self) -> int | None:
        return None

    @property
    def lease_deadline(self) -> datetime | None:
        return None

    async def in_progress(self) -> None:
        await self.extend(ACK_WAIT_SECONDS)

    async def extend(self, seconds: float) -> bool:
        del seconds  # JetStream renews by its configured ack wait.
        await self.message.in_progress()
        return True

    async def ack(self) -> None:
        await self.acknowledge()

    async def acknowledge(self) -> None:
        try:
            await self.message.ack_sync(timeout=5.0)
        except Exception:
            metrics.REGISTRY.counter(
                "catalogue_queue_ack_failures_total",
                "Confirmed JetStream acknowledgements that failed.",
                route=self.envelope.route,
            )
            raise

    async def retry(self, delay: float) -> None:
        await self.message.nak(delay=max(delay, 0.0))

    async def reject(self, reason: str = "invalid envelope") -> None:
        del reason
        await self.message.term()


class NatsJobQueue:
    """One connection shared by a worker or dispatcher process."""

    provider = "nats"

    def __init__(
        self,
        url: str,
        *,
        token: str = "",
        user: str = "",
        password: str = "",
        stream: str = STREAM,
        subject_prefix: str = SUBJECT_PREFIX,
    ) -> None:
        self.url = url
        self.token = token
        self.user = user
        self.password = password
        if self.token and (self.user or self.password):
            raise ValueError("NATS token and user/password authentication are mutually exclusive")
        if bool(self.user) != bool(self.password):
            raise ValueError("NATS user and password must be supplied together")
        self.stream = stream
        self.subject_prefix = subject_prefix
        self._nc: NATS | None = None
        self._js: JetStreamContext | None = None
        self._subscriptions: dict[str, JetStreamContext.PullSubscription] = {}

    async def connect(self) -> None:
        if self._nc is not None and self._nc.is_connected:
            return
        options: dict[str, Any] = {
            "servers": [self.url],
            "connect_timeout": 5,
            "max_reconnect_attempts": -1,
            "reconnect_time_wait": 1,
            "name": "catalogue-jobs",
        }
        if self.token:
            options["token"] = self.token
        elif self.user:
            options["user"] = self.user
            options["password"] = self.password
        self._nc = await nats.connect(**options)
        self._js = self._nc.jetstream()

    async def provision(self) -> None:
        js = self._require_js()
        desired = StreamConfig(
            name=self.stream,
            subjects=[f"{self.subject_prefix}.>"],
            retention=RetentionPolicy.WORK_QUEUE,
            storage=StorageType.FILE,
            max_age=timedelta(days=14).total_seconds(),
            duplicate_window=timedelta(hours=24).total_seconds(),
            max_msg_size=16 * 1024,
        )
        try:
            await js.stream_info(self.stream)
            await js.update_stream(desired)
        except NotFoundError:
            await js.add_stream(desired)

        for route in ROUTES:
            durable = durable_for(route)
            config = ConsumerConfig(
                durable_name=durable,
                deliver_policy=DeliverPolicy.ALL,
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=ACK_WAIT_SECONDS,
                max_deliver=-1,
                max_ack_pending=64,
                filter_subject=self.subject(route),
            )
            # JetStream's create-consumer request is also the idempotent update
            # operation for a named durable.
            await js.add_consumer(self.stream, config=config)

    async def publish(self, envelope: JobEnvelope) -> PublishReceipt:
        js = self._require_js()
        ack = await js.publish(
            self.subject(envelope.route),
            envelope.encode(),
            headers={"Nats-Msg-Id": envelope.deduplication_key},
            timeout=5.0,
        )
        return PublishReceipt(provider_message_id=str(ack.seq), duplicate=bool(ack.duplicate))

    async def deliveries(self, routes: Sequence[str]) -> AsyncIterator[Delivery]:
        if not routes:
            raise ValueError("at least one NATS job route is required")
        await self.connect()
        position = 0
        while True:
            route = routes[position % len(routes)]
            position += 1
            subscription = await self._subscription(route)
            try:
                messages = await subscription.fetch(batch=1, timeout=1.0)
            except (TimeoutError, NatsTimeoutError):
                continue
            for message in messages:
                try:
                    envelope = JobEnvelope.decode(message.data)
                    if message.subject != self.subject(envelope.route):
                        raise ValueError("job envelope route does not match its NATS subject")
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    LOGGER.error("queue.message_invalid", subject=message.subject, exc_info=True)
                    metrics.REGISTRY.counter(
                        "catalogue_queue_invalid_messages_total",
                        "Broker messages terminated because their envelope was invalid.",
                        subject=message.subject,
                    )
                    await message.term()
                    continue
                yield Delivery(envelope, message)

    async def next_delivery(self, routes: Sequence[str]) -> Delivery | None:
        """Poll every compatible disjoint route once."""
        if not routes:
            raise ValueError("at least one NATS job route is required")
        await self.connect()
        for route in routes:
            subscription = await self._subscription(route)
            try:
                messages = await subscription.fetch(batch=1, timeout=0.25)
            except (TimeoutError, NatsTimeoutError):
                continue
            message = messages[0]
            try:
                envelope = JobEnvelope.decode(message.data)
                if message.subject != self.subject(envelope.route):
                    raise ValueError("job envelope route does not match its NATS subject")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                LOGGER.error("queue.message_invalid", subject=message.subject, exc_info=True)
                metrics.REGISTRY.counter(
                    "catalogue_queue_invalid_messages_total",
                    "Broker messages terminated because their envelope was invalid.",
                    subject=message.subject,
                )
                await message.term()
                continue
            return Delivery(envelope, message)
        return None

    async def stats(self) -> dict[str, dict[str, int]]:
        js = self._require_js()
        result: dict[str, dict[str, int]] = {}
        for route in ROUTES:
            info = await js.consumer_info(self.stream, durable_for(route))
            result[route] = {
                "ready": int(info.num_pending or 0),
                "in_flight": int(info.num_ack_pending or 0),
                "redelivered": int(info.num_redelivered or 0),
            }
        return result

    async def close(self) -> None:
        self._subscriptions.clear()
        if self._nc is not None:
            await self._nc.drain()
            await self._nc.close()
        self._nc = None
        self._js = None

    def subject(self, route: str) -> str:
        if route not in ROUTES:
            raise ValueError(f"unsupported job route {route!r}")
        return f"{self.subject_prefix}.{route}"

    async def _subscription(self, route: str) -> JetStreamContext.PullSubscription:
        found = self._subscriptions.get(route)
        if found is not None:
            return found
        js = self._require_js()
        found = await js.pull_subscribe(self.subject(route), durable=durable_for(route), stream=self.stream)
        self._subscriptions[route] = found
        return found

    def _require_js(self) -> JetStreamContext:
        if self._js is None:
            raise RuntimeError("NATS job queue is not connected")
        return self._js


def durable_for(route: str) -> str:
    if route not in ROUTES:
        raise ValueError(f"unsupported job route {route!r}")
    return f"catalogue-{route.replace('.', '-').replace('_', '-')}"
