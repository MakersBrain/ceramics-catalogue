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
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

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

LOGGER = obs.get_logger("catalogue.nats")

STREAM = "CATALOGUE_JOBS"
SUBJECT_PREFIX = "catalogue.jobs.v1"
SCHEMA = "catalogue.job.v1"
ACK_WAIT_SECONDS = 30.0

ROUTES = (
    "plain.normal",
    "browser.auto.normal",
    "browser.camoufox.normal",
    "browser.cdp_extension_proxy.normal",
)


@dataclass(frozen=True)
class JobEnvelope:
    job_id: UUID
    run_id: UUID
    source_id: str
    generation: int
    route: str
    priority: int
    enqueued_at: datetime

    def encode(self) -> bytes:
        return json.dumps(
            {
                "schema": SCHEMA,
                "job_id": str(self.job_id),
                "run_id": str(self.run_id),
                "source_id": self.source_id,
                "generation": self.generation,
                "route": self.route,
                "priority": self.priority,
                "enqueued_at": self.enqueued_at.astimezone(UTC).isoformat(),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()

    @classmethod
    def decode(cls, payload: bytes) -> JobEnvelope:
        raw = json.loads(payload)
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
            raise ValueError("unsupported job envelope schema")
        route = str(raw["route"])
        if route not in ROUTES:
            raise ValueError(f"unsupported job route {route!r}")
        generation = int(raw["generation"])
        if generation < 1:
            raise ValueError("job generation must be positive")
        return cls(
            job_id=UUID(str(raw["job_id"])),
            run_id=UUID(str(raw["run_id"])),
            source_id=str(raw["source_id"]),
            generation=generation,
            route=route,
            priority=int(raw["priority"]),
            enqueued_at=datetime.fromisoformat(str(raw["enqueued_at"])),
        )


@dataclass
class Delivery:
    envelope: JobEnvelope
    message: Msg

    async def in_progress(self) -> None:
        await self.message.in_progress()

    async def ack(self) -> None:
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

    async def reject(self) -> None:
        await self.message.term()


class NatsJobQueue:
    """One connection shared by a worker or dispatcher process."""

    def __init__(
        self,
        url: str,
        *,
        token: str = "",
        stream: str = STREAM,
        subject_prefix: str = SUBJECT_PREFIX,
    ) -> None:
        self.url = url
        self.token = token
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
        self._nc = await nats.connect(**options)
        self._js = self._nc.jetstream()
        await self.provision()

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

    async def publish(self, envelope: JobEnvelope) -> None:
        js = self._require_js()
        await js.publish(
            self.subject(envelope.route),
            envelope.encode(),
            headers={"Nats-Msg-Id": f"{envelope.job_id}:{envelope.generation}"},
            timeout=5.0,
        )

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
        found = await js.pull_subscribe(
            self.subject(route), durable=durable_for(route), stream=self.stream
        )
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


def routes_for(capabilities: Sequence[str]) -> list[str]:
    available = set(capabilities)
    routes = ["plain.normal"]
    exact = {
        value.removeprefix("browser:") for value in available if value.startswith("browser:")
    }
    if exact:
        routes.insert(0, "browser.auto.normal")
    if "camoufox" in exact:
        routes.insert(0, "browser.camoufox.normal")
    if "cdp_extension_proxy" in exact:
        routes.insert(0, "browser.cdp_extension_proxy.normal")
    return routes
