"""Provider-neutral work-delivery contracts and observable queue semantics."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Generic, Protocol, TypeVar
from uuid import UUID

SCHEMA = "catalogue.job.v1"
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

    @property
    def schema(self) -> str:
        return SCHEMA

    @property
    def deduplication_key(self) -> str:
        return f"{self.job_id}:{self.generation}"

    def encode(self) -> bytes:
        return json.dumps(
            {
                "schema": self.schema,
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
    def decode(cls, payload: bytes | str) -> JobEnvelope:
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
            enqueued_at=datetime.fromisoformat(str(raw["enqueued_at"])).astimezone(UTC),
        )


@dataclass(frozen=True)
class PublishReceipt:
    provider_message_id: str | None = None
    duplicate: bool | None = None


class Delivery(Protocol):
    envelope: JobEnvelope
    provider_message_id: str | None
    delivery_attempt: int | None
    remaining_delivery_attempts: int | None
    lease_deadline: datetime | None

    async def acknowledge(self) -> None: ...
    async def retry(self, delay_seconds: float) -> None: ...
    async def reject(self, reason: str = "invalid envelope") -> None: ...
    async def extend(self, seconds: float) -> bool: ...


class QueuePublisher(Protocol):
    provider: str

    async def connect(self) -> None: ...
    async def publish(self, envelope: JobEnvelope) -> PublishReceipt: ...
    async def close(self) -> None: ...


class QueueConsumer(Protocol):
    provider: str

    async def connect(self) -> None: ...
    async def next_delivery(self, routes: Sequence[str]) -> Delivery | None: ...
    def deliveries(self, routes: Sequence[str]) -> AsyncIterator[Delivery]: ...
    async def close(self) -> None: ...


class QueueProvisioner(Protocol):
    provider: str

    async def validate(self, routes: Sequence[str] = ROUTES) -> list[str]: ...
    async def apply(self, routes: Sequence[str] = ROUTES) -> None: ...
    async def purge(self, routes: Sequence[str] = ROUTES) -> None: ...
    async def close(self) -> None: ...


class Accuracy(StrEnum):
    EXACT = "exact"
    BEST_EFFORT = "best_effort"
    UNSUPPORTED = "unsupported"


T = TypeVar("T", int, float)


@dataclass(frozen=True)
class Measurement(Generic[T]):
    value: T | None
    accuracy: Accuracy

    @classmethod
    def exact(cls, value: T) -> Measurement[T]:
        return cls(value=value, accuracy=Accuracy.EXACT)

    @classmethod
    def best_effort(cls, value: T) -> Measurement[T]:
        return cls(value=value, accuracy=Accuracy.BEST_EFFORT)

    @classmethod
    def unsupported(cls) -> Measurement[T]:
        return cls(value=None, accuracy=Accuracy.UNSUPPORTED)

    def json(self) -> dict[str, int | float | str | None]:
        return {"value": self.value, "accuracy": self.accuracy.value}


@dataclass(frozen=True)
class QueueRouteSnapshot:
    route: str
    ready: Measurement[int]
    in_flight: Measurement[int]
    redelivered: Measurement[int]
    delivered: Measurement[int]
    oldest_age_seconds: Measurement[float]


@dataclass(frozen=True)
class QueueRecoverySnapshot:
    backlog_messages: Measurement[int]
    oldest_age_seconds: Measurement[float]


@dataclass(frozen=True)
class QueueSnapshot:
    provider: str
    observed_at: datetime
    last_success_at: datetime | None
    available: bool
    backlog_messages: Measurement[int]
    backlog_bytes: Measurement[int]
    consumer_count: Measurement[int]
    routes: tuple[QueueRouteSnapshot, ...]
    recovery_dlq: QueueRecoverySnapshot | None = None
    error: str | None = None

    def json(self) -> dict[str, object]:
        def route_json(route: QueueRouteSnapshot) -> dict[str, object]:
            return {
                "route": route.route,
                "ready": route.ready.json(),
                "in_flight": route.in_flight.json(),
                "redelivered": route.redelivered.json(),
                "delivered": route.delivered.json(),
                "oldest_age_seconds": route.oldest_age_seconds.json(),
            }

        recovery = None
        if self.recovery_dlq is not None:
            recovery = {
                "backlog_messages": self.recovery_dlq.backlog_messages.json(),
                "oldest_age_seconds": self.recovery_dlq.oldest_age_seconds.json(),
            }
        return {
            "provider": self.provider,
            "observed_at": self.observed_at,
            "last_success_at": self.last_success_at,
            "available": self.available,
            "backlog_messages": self.backlog_messages.json(),
            "backlog_bytes": self.backlog_bytes.json(),
            "consumer_count": self.consumer_count.json(),
            "routes": [route_json(route) for route in self.routes],
            "recovery_dlq": recovery,
            "error": self.error,
        }


class QueueStatsReader(Protocol):
    provider: str

    async def snapshot(self) -> QueueSnapshot: ...
    async def close(self) -> None: ...


def routes_for(capabilities: Sequence[str]) -> list[str]:
    available = set(capabilities)
    routes = ["plain.normal"]
    exact = {value.removeprefix("browser:") for value in available if value.startswith("browser:")}
    if exact:
        routes.insert(0, "browser.auto.normal")
    if "camoufox" in exact:
        routes.insert(0, "browser.camoufox.normal")
    if "cdp_extension_proxy" in exact:
        routes.insert(0, "browser.cdp_extension_proxy.normal")
    return routes
