"""Cloudflare Queues HTTP publisher, pull consumer, and normalized statistics."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from mb_ceramics_catalogue.observability import logging as obs
from mb_ceramics_catalogue.observability import metrics
from mb_ceramics_catalogue.ops.delivery import (
    ROUTES,
    JobEnvelope,
    Measurement,
    PublishReceipt,
    QueueRecoverySnapshot,
    QueueRouteSnapshot,
    QueueSnapshot,
)

LOGGER = obs.get_logger("catalogue.cloudflare_queue")


class CloudflareQueueError(RuntimeError):
    """A sanitized Cloudflare API failure."""


class CloudflareQueueClient:
    def __init__(
        self,
        account_id: str,
        token: str,
        *,
        base_url: str = "https://api.cloudflare.com/client/v4",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.account_id = account_id
        self._token = token
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
            timeout=httpx.Timeout(10.0),
        )

    def path(self, queue_id: str, action: str = "") -> str:
        suffix = f"/{action.lstrip('/')}" if action else ""
        return f"/accounts/{self.account_id}/queues/{queue_id}{suffix}"

    async def request(
        self, method: str, queue_id: str, action: str = "", *, json_body: object | None = None
    ) -> dict[str, Any]:
        try:
            response = await self.client.request(method, self.path(queue_id, action), json=json_body)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise CloudflareQueueError(
                f"Cloudflare Queues {method} {action or 'queue'} failed: {type(error).__name__}"
            ) from error
        if not isinstance(payload, dict) or payload.get("success") is not True:
            codes = [str(item.get("code")) for item in payload.get("errors", []) if isinstance(item, dict)]
            detail = f" error codes {','.join(codes)}" if codes else ""
            raise CloudflareQueueError(f"Cloudflare Queues rejected {action or 'queue'}{detail}")
        return payload

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()


class CloudflarePublisher:
    provider = "cloudflare"

    def __init__(self, api: CloudflareQueueClient, route_queues: Mapping[str, str]) -> None:
        self.api = api
        self.route_queues = _validate_routes(route_queues)

    async def connect(self) -> None:
        return None

    async def publish(self, envelope: JobEnvelope) -> PublishReceipt:
        queue_id = self.route_queues.get(envelope.route)
        if queue_id is None:
            raise ValueError(f"unsupported job route {envelope.route!r}")
        payload = await self.api.request(
            "POST",
            queue_id,
            "messages",
            json_body={"body": json.loads(envelope.encode()), "content_type": "json"},
        )
        result = payload.get("result")
        message_id = result.get("id") if isinstance(result, dict) else None
        return PublishReceipt(
            provider_message_id=str(message_id) if message_id is not None else None,
            duplicate=None,
        )

    async def close(self) -> None:
        await self.api.close()


@dataclass
class CloudflareDelivery:
    envelope: JobEnvelope
    api: CloudflareQueueClient
    queue_id: str
    lease_id: str
    provider_message_id: str | None
    delivery_attempt: int | None
    remaining_delivery_attempts: int | None
    lease_deadline: datetime | None

    async def _settle(self, kind: str, delay_seconds: float | None = None) -> None:
        item: dict[str, object] = {"lease_id": self.lease_id}
        if delay_seconds is not None:
            item["delay_seconds"] = max(0, min(int(delay_seconds), 86_400))
        body = {"acks": [item] if kind == "acks" else [], "retries": [item] if kind == "retries" else []}
        response = await self.api.request("POST", self.queue_id, "messages/ack", json_body=body)
        result = response.get("result")
        if isinstance(result, dict):
            warnings = result.get("warnings")
            expected = "ackCount" if kind == "acks" else "retryCount"
            count = result.get(expected)
            if (isinstance(warnings, dict) and self.lease_id in warnings) or (
                count is not None and int(count) != 1
            ):
                raise CloudflareQueueError(f"Cloudflare Queues did not confirm message {kind}")

    async def acknowledge(self) -> None:
        try:
            await self._settle("acks")
        except Exception:
            metrics.REGISTRY.counter(
                "catalogue_queue_ack_failures_total",
                "Confirmed provider acknowledgements that failed.",
                provider="cloudflare",
                route=self.envelope.route,
            )
            raise

    async def ack(self) -> None:
        await self.acknowledge()

    async def retry(self, delay_seconds: float) -> None:
        if self.remaining_delivery_attempts is not None and self.remaining_delivery_attempts <= 1:
            metrics.REGISTRY.counter(
                "catalogue_queue_retry_budget_low_total",
                "Deliveries that approached a provider retry limit.",
                provider="cloudflare",
                route=self.envelope.route,
            )
        await self._settle("retries", delay_seconds)

    async def reject(self, reason: str = "invalid envelope") -> None:
        LOGGER.warning(
            "queue.message_rejected",
            provider="cloudflare",
            route=self.envelope.route,
            reason=reason,
        )
        await self.acknowledge()

    async def extend(self, seconds: float) -> bool:
        del seconds
        return False

    async def in_progress(self) -> None:
        # The worker validates a fixed visibility bound at startup. Cloudflare
        # does not currently expose pull-lease extension.
        return None


class CloudflareConsumer:
    provider = "cloudflare"

    def __init__(
        self,
        api: CloudflareQueueClient,
        route_queues: Mapping[str, str],
        *,
        visibility_seconds: int,
        max_retries: int = 100,
        empty_poll_seconds: float = 2.0,
    ) -> None:
        self.api = api
        self.route_queues = _validate_routes(route_queues)
        self.visibility_seconds = visibility_seconds
        self.max_retries = max_retries
        self.empty_poll_seconds = empty_poll_seconds

    async def connect(self) -> None:
        return None

    async def next_delivery(self, routes: Sequence[str]) -> CloudflareDelivery | None:
        if not routes:
            raise ValueError("at least one queue route is required")
        for route in routes:
            queue_id = self.route_queues.get(route)
            if queue_id is None:
                raise ValueError(f"unsupported job route {route!r}")
            payload = await self.api.request(
                "POST",
                queue_id,
                "messages/pull",
                json_body={
                    "visibility_timeout_ms": self.visibility_seconds * 1000,
                    "batch_size": 1,
                },
            )
            result = payload.get("result")
            messages = result.get("messages", []) if isinstance(result, dict) else []
            if not messages:
                continue
            raw = messages[0]
            if not isinstance(raw, dict):
                raise CloudflareQueueError("Cloudflare pull returned an invalid message")
            lease_id = str(raw.get("lease_id") or "")
            if not lease_id:
                raise CloudflareQueueError("Cloudflare pull returned no lease id")
            try:
                envelope = JobEnvelope.decode(_decode_body(raw))
                if envelope.route != route:
                    raise ValueError("job envelope route does not match its queue")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                await self.api.request(
                    "POST",
                    queue_id,
                    "messages/ack",
                    json_body={"acks": [{"lease_id": lease_id}], "retries": []},
                )
                metrics.REGISTRY.counter(
                    "catalogue_queue_invalid_messages_total",
                    "Broker messages terminated because their envelope was invalid.",
                    provider="cloudflare",
                    route=route,
                )
                LOGGER.error("queue.message_invalid", provider="cloudflare", route=route)
                continue
            attempts = int(raw["attempts"]) if raw.get("attempts") is not None else None
            remaining = max(0, self.max_retries - attempts) if attempts is not None else None
            return CloudflareDelivery(
                envelope=envelope,
                api=self.api,
                queue_id=queue_id,
                lease_id=lease_id,
                provider_message_id=str(raw["id"]) if raw.get("id") is not None else None,
                delivery_attempt=attempts,
                remaining_delivery_attempts=remaining,
                lease_deadline=datetime.now(UTC) + timedelta(seconds=self.visibility_seconds),
            )
        return None

    async def deliveries(self, routes: Sequence[str]) -> AsyncIterator[CloudflareDelivery]:
        while True:
            delivery = await self.next_delivery(routes)
            if delivery is None:
                await asyncio.sleep(self.empty_poll_seconds)
                continue
            yield delivery

    async def close(self) -> None:
        await self.api.close()


class CloudflareRecoveryConsumer:
    """Pull only the recovery DLQ; it never executes an envelope."""

    provider = "cloudflare"

    def __init__(
        self,
        api: CloudflareQueueClient,
        recovery_dlq_id: str,
        *,
        visibility_seconds: int,
    ) -> None:
        self.api = api
        self.recovery_dlq_id = recovery_dlq_id
        self.visibility_seconds = visibility_seconds

    async def connect(self) -> None:
        return None

    async def next_delivery(self, routes: Sequence[str] = ROUTES) -> CloudflareDelivery | None:
        payload = await self.api.request(
            "POST",
            self.recovery_dlq_id,
            "messages/pull",
            json_body={"visibility_timeout_ms": self.visibility_seconds * 1000, "batch_size": 1},
        )
        result = payload.get("result")
        messages = result.get("messages", []) if isinstance(result, dict) else []
        if not messages:
            return None
        raw = messages[0]
        if not isinstance(raw, dict) or not raw.get("lease_id"):
            raise CloudflareQueueError("Cloudflare recovery pull returned an invalid message")
        lease_id = str(raw["lease_id"])
        try:
            envelope = JobEnvelope.decode(_decode_body(raw))
            if envelope.route not in routes:
                raise ValueError("unsupported recovery route")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            await self.api.request(
                "POST",
                self.recovery_dlq_id,
                "messages/ack",
                json_body={"acks": [{"lease_id": lease_id}], "retries": []},
            )
            metrics.REGISTRY.counter(
                "catalogue_queue_invalid_messages_total",
                "Broker messages terminated because their envelope was invalid.",
                provider="cloudflare",
                route="recovery",
            )
            LOGGER.error("queue.recovery_message_invalid", provider="cloudflare")
            return None
        return CloudflareDelivery(
            envelope=envelope,
            api=self.api,
            queue_id=self.recovery_dlq_id,
            lease_id=lease_id,
            provider_message_id=str(raw["id"]) if raw.get("id") is not None else None,
            delivery_attempt=int(raw["attempts"]) if raw.get("attempts") is not None else None,
            remaining_delivery_attempts=None,
            lease_deadline=datetime.now(UTC) + timedelta(seconds=self.visibility_seconds),
        )

    async def deliveries(self, routes: Sequence[str] = ROUTES) -> AsyncIterator[CloudflareDelivery]:
        while True:
            delivery = await self.next_delivery(routes)
            if delivery is None:
                await asyncio.sleep(2)
                continue
            yield delivery

    async def close(self) -> None:
        await self.api.close()


class CloudflareStatsReader:
    provider = "cloudflare"

    def __init__(
        self,
        api: CloudflareQueueClient,
        route_queues: Mapping[str, str],
        recovery_dlq_id: str,
    ) -> None:
        self.api = api
        self.route_queues = _validate_routes(route_queues)
        self.recovery_dlq_id = recovery_dlq_id
        self._last_success_at: datetime | None = None

    async def snapshot(self) -> QueueSnapshot:
        observed = datetime.now(UTC)
        try:
            route_metrics = await asyncio.gather(
                *(self._metrics(route, queue_id) for route, queue_id in self.route_queues.items())
            )
            recovery_metrics = await self._raw_metrics(self.recovery_dlq_id)
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
                error=f"{type(error).__name__}: queue statistics unavailable",
            )
        self._last_success_at = observed
        return QueueSnapshot(
            provider=self.provider,
            observed_at=observed,
            last_success_at=observed,
            available=True,
            backlog_messages=Measurement.best_effort(
                sum(int(row[1].get("backlog_count") or 0) for row in route_metrics)
            ),
            backlog_bytes=Measurement.best_effort(
                sum(int(row[1].get("backlog_bytes") or 0) for row in route_metrics)
            ),
            consumer_count=Measurement.unsupported(),
            routes=tuple(row[0] for row in route_metrics),
            recovery_dlq=QueueRecoverySnapshot(
                backlog_messages=Measurement.best_effort(int(recovery_metrics.get("backlog_count") or 0)),
                oldest_age_seconds=Measurement.best_effort(_oldest_age(recovery_metrics, observed)),
            ),
        )

    async def _metrics(self, route: str, queue_id: str) -> tuple[QueueRouteSnapshot, dict[str, Any]]:
        raw = await self._raw_metrics(queue_id)
        return (
            QueueRouteSnapshot(
                route=route,
                ready=Measurement.best_effort(int(raw.get("backlog_count") or 0)),
                in_flight=Measurement.unsupported(),
                redelivered=Measurement.unsupported(),
                delivered=Measurement.unsupported(),
                oldest_age_seconds=Measurement.best_effort(_oldest_age(raw, datetime.now(UTC))),
            ),
            raw,
        )

    async def _raw_metrics(self, queue_id: str) -> dict[str, Any]:
        payload = await self.api.request("GET", queue_id, "metrics")
        result = payload.get("result")
        return result if isinstance(result, dict) else {}

    async def close(self) -> None:
        await self.api.close()


class CloudflareProvisioner:
    """Validate and configure HTTP-pull consumers for every route queue."""

    provider = "cloudflare"

    def __init__(
        self,
        api: CloudflareQueueClient,
        route_queues: Mapping[str, str],
        recovery_dlq_id: str,
        *,
        visibility_seconds: int,
        max_retries: int,
    ) -> None:
        self.api = api
        self.route_queues = _validate_routes(route_queues)
        self.recovery_dlq_id = recovery_dlq_id
        self.visibility_seconds = visibility_seconds
        self.max_retries = max_retries

    async def validate(self, routes: Sequence[str] = ROUTES) -> list[str]:
        self._require_complete(routes)
        issues: list[str] = []
        try:
            recovery = await self.api.request("GET", self.recovery_dlq_id)
            recovery_name = _queue_name(recovery)
        except CloudflareQueueError:
            return ["recovery DLQ is unavailable"]
        for route, queue_id in self.route_queues.items():
            try:
                await self.api.request("GET", queue_id)
                consumers = await self._consumers(queue_id)
            except CloudflareQueueError:
                issues.append(f"route {route} queue is unavailable")
                continue
            pulls = [item for item in consumers if item.get("type") == "http_pull"]
            if len(pulls) != 1:
                issues.append(f"route {route} requires exactly one HTTP pull consumer")
                continue
            issues.extend(self._consumer_issues(route, pulls[0], recovery_name))
        try:
            recovery_consumers = await self._consumers(self.recovery_dlq_id)
        except CloudflareQueueError:
            issues.append("recovery DLQ consumers are unavailable")
        else:
            pulls = [item for item in recovery_consumers if item.get("type") == "http_pull"]
            if len(pulls) != 1:
                issues.append("recovery DLQ requires exactly one HTTP pull consumer")
        return issues

    async def apply(self, routes: Sequence[str] = ROUTES) -> None:
        self._require_complete(routes)
        recovery = await self.api.request("GET", self.recovery_dlq_id)
        recovery_name = _queue_name(recovery)
        for queue_id in self.route_queues.values():
            await self._upsert_pull_consumer(queue_id, dead_letter_queue=recovery_name)
        await self._upsert_pull_consumer(self.recovery_dlq_id, dead_letter_queue=None)

    async def purge(self, routes: Sequence[str] = ROUTES) -> None:
        self._require_complete(routes)
        for queue_id in (*self.route_queues.values(), self.recovery_dlq_id):
            await self.api.request(
                "POST",
                queue_id,
                "purge",
                json_body={"delete_messages_permanently": True},
            )

    async def close(self) -> None:
        await self.api.close()

    async def _consumers(self, queue_id: str) -> list[dict[str, Any]]:
        payload = await self.api.request("GET", queue_id, "consumers")
        result = payload.get("result")
        if not isinstance(result, list):
            raise CloudflareQueueError("Cloudflare Queues returned invalid consumers")
        return [item for item in result if isinstance(item, dict)]

    async def _upsert_pull_consumer(self, queue_id: str, *, dead_letter_queue: str | None) -> None:
        consumers = await self._consumers(queue_id)
        pulls = [item for item in consumers if item.get("type") == "http_pull"]
        if len(pulls) > 1:
            raise CloudflareQueueError("Cloudflare queue has multiple HTTP pull consumers")
        body: dict[str, object] = {
            "type": "http_pull",
            "settings": {
                "batch_size": 1,
                "max_retries": self.max_retries,
                "visibility_timeout_ms": self.visibility_seconds * 1000,
            },
        }
        if dead_letter_queue is not None:
            body["dead_letter_queue"] = dead_letter_queue
        if pulls:
            consumer_id = str(pulls[0].get("consumer_id") or "")
            if not consumer_id:
                raise CloudflareQueueError("Cloudflare HTTP pull consumer has no id")
            await self.api.request("PUT", queue_id, f"consumers/{consumer_id}", json_body=body)
        else:
            await self.api.request("POST", queue_id, "consumers", json_body=body)

    def _consumer_issues(self, route: str, consumer: Mapping[str, Any], recovery_name: str) -> list[str]:
        issues: list[str] = []
        settings = consumer.get("settings")
        values = settings if isinstance(settings, dict) else {}
        expected = {
            "batch_size": 1,
            "max_retries": self.max_retries,
            "visibility_timeout_ms": self.visibility_seconds * 1000,
        }
        for name, value in expected.items():
            if values.get(name) != value:
                issues.append(f"route {route} consumer {name} must be {value}")
        if consumer.get("dead_letter_queue") != recovery_name:
            issues.append(f"route {route} consumer recovery DLQ is incorrect")
        return issues

    @staticmethod
    def _require_complete(routes: Sequence[str]) -> None:
        if tuple(routes) != ROUTES:
            raise ValueError("Cloudflare provisioning requires the complete route registry")


def _validate_routes(route_queues: Mapping[str, str]) -> dict[str, str]:
    missing = [route for route in ROUTES if not route_queues.get(route)]
    unknown = [route for route in route_queues if route not in ROUTES]
    if missing or unknown:
        raise ValueError(f"invalid Cloudflare route mapping; missing={missing}, unknown={unknown}")
    return dict(route_queues)


def _queue_name(payload: Mapping[str, Any]) -> str:
    result = payload.get("result")
    name = result.get("queue_name") if isinstance(result, dict) else None
    if not name:
        raise CloudflareQueueError("Cloudflare recovery DLQ has no queue name")
    return str(name)


def _decode_body(raw: Mapping[str, Any]) -> bytes:
    body = raw.get("body")
    metadata = raw.get("metadata")
    content_type = metadata.get("CF-Content-Type") if isinstance(metadata, dict) else None
    if content_type in ("json", "bytes") and isinstance(body, str):
        return base64.b64decode(body, validate=True)
    if isinstance(body, str):
        return body.encode()
    return json.dumps(body, separators=(",", ":")).encode()


def _oldest_age(raw: Mapping[str, Any], now: datetime) -> float:
    timestamp = int(raw.get("oldest_message_timestamp_ms") or 0)
    if not timestamp:
        return 0.0
    oldest = datetime.fromtimestamp(timestamp / 1000, tz=UTC)
    return max(0.0, (now - oldest).total_seconds())
