from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from mb_ceramics_catalogue.ops.delivery import JobEnvelope
from mb_ceramics_catalogue.ops.providers.cloudflare import (
    CloudflareConsumer,
    CloudflareProvisioner,
    CloudflarePublisher,
    CloudflareQueueClient,
    CloudflareQueueError,
    CloudflareRecoveryConsumer,
    CloudflareStatsReader,
)

ROUTES = {
    "plain.normal": "plain-id",
    "browser.auto.normal": "auto-id",
    "browser.camoufox.normal": "camoufox-id",
    "browser.cdp_extension_proxy.normal": "cdp-id",
}


def envelope(route: str = "plain.normal") -> JobEnvelope:
    return JobEnvelope(
        job_id=uuid4(),
        run_id=uuid4(),
        source_id="shop",
        generation=2,
        route=route,
        priority=50,
        enqueued_at=datetime.now(UTC),
    )


def api(handler) -> CloudflareQueueClient:
    client = httpx.AsyncClient(
        base_url="https://queue.test/client/v4", transport=httpx.MockTransport(handler)
    )
    return CloudflareQueueClient("account", "secret", base_url=str(client.base_url), client=client)


async def test_publish_maps_route_and_sends_json_envelope() -> None:
    found: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        found.append(request)
        return httpx.Response(200, json={"success": True, "result": {}})

    publisher = CloudflarePublisher(api(handler), ROUTES)
    job = envelope()
    await publisher.publish(job)

    assert found[0].url.path.endswith("/queues/plain-id/messages")
    body = json.loads(found[0].content)
    assert body["content_type"] == "json"
    assert body["body"]["job_id"] == str(job.job_id)


async def test_pull_decodes_json_and_acknowledges_by_lease() -> None:
    job = envelope()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/messages/pull"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {
                        "messages": [
                            {
                                "id": "message-1",
                                "lease_id": "lease-1",
                                "attempts": 4,
                                "body": base64.b64encode(job.encode()).decode(),
                                "metadata": {"CF-Content-Type": "json"},
                            }
                        ]
                    },
                },
            )
        return httpx.Response(200, json={"success": True, "result": {}})

    consumer = CloudflareConsumer(api(handler), ROUTES, visibility_seconds=4200)
    delivery = await consumer.next_delivery(["plain.normal"])
    assert delivery is not None
    assert delivery.envelope == job
    assert delivery.delivery_attempt == 4
    assert delivery.remaining_delivery_attempts == 96
    assert await delivery.extend(5) is False
    await delivery.acknowledge()

    ack = json.loads(requests[-1].content)
    assert ack == {"acks": [{"lease_id": "lease-1"}], "retries": []}


async def test_retry_carries_bounded_delay() -> None:
    job = envelope()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/messages/pull"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {
                        "messages": [
                            {
                                "lease_id": "lease-2",
                                "attempts": 99,
                                "body": base64.b64encode(job.encode()).decode(),
                                "metadata": {"CF-Content-Type": "json"},
                            }
                        ]
                    },
                },
            )
        return httpx.Response(200, json={"success": True, "result": {}})

    consumer = CloudflareConsumer(api(handler), ROUTES, visibility_seconds=4200)
    delivery = await consumer.next_delivery(["plain.normal"])
    assert delivery is not None
    await delivery.retry(100_000)
    retry = json.loads(requests[-1].content)
    assert retry == {"acks": [], "retries": [{"lease_id": "lease-2", "delay_seconds": 86_400}]}


async def test_ack_warning_is_not_treated_as_confirmation() -> None:
    job = envelope()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages/pull"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {
                        "messages": [
                            {
                                "lease_id": "expired-lease",
                                "body": job.encode().decode(),
                                "attempts": 1,
                            }
                        ]
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {"ackCount": 0, "warnings": {"expired-lease": "expired"}},
            },
        )

    consumer = CloudflareConsumer(api(handler), ROUTES, visibility_seconds=4200)
    delivery = await consumer.next_delivery(["plain.normal"])
    assert delivery is not None
    with pytest.raises(CloudflareQueueError, match="did not confirm"):
        await delivery.acknowledge()


async def test_invalid_recovery_message_is_terminally_acknowledged() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/messages/pull"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {"messages": [{"lease_id": "bad-lease", "body": "not-json"}]},
                },
            )
        return httpx.Response(200, json={"success": True, "result": {}})

    consumer = CloudflareRecoveryConsumer(api(handler), "recovery-id", visibility_seconds=4200)
    assert await consumer.next_delivery() is None
    assert json.loads(requests[-1].content) == {
        "acks": [{"lease_id": "bad-lease"}],
        "retries": [],
    }


async def test_stats_are_best_effort_and_keep_unsupported_values_explicit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        queue_id = request.url.path.split("/queues/")[1].split("/")[0]
        count = 2 if queue_id == "plain-id" else 1 if queue_id == "recovery-id" else 0
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "backlog_count": count,
                    "backlog_bytes": count * 100,
                    "oldest_message_timestamp_ms": 0,
                },
            },
        )

    snapshot = await CloudflareStatsReader(api(handler), ROUTES, "recovery-id").snapshot()
    assert snapshot.available is True
    assert snapshot.backlog_messages.value == 2
    assert snapshot.backlog_messages.accuracy.value == "best_effort"
    assert snapshot.routes[0].in_flight.value is None
    assert snapshot.routes[0].in_flight.accuracy.value == "unsupported"
    assert snapshot.recovery_dlq is not None
    assert snapshot.recovery_dlq.backlog_messages.value == 1


async def test_partial_stats_rate_limit_marks_snapshot_unavailable_without_secret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/queues/auto-id/" in request.url.path:
            return httpx.Response(429, text="token secret should not escape")
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "backlog_count": 0,
                    "backlog_bytes": 0,
                    "oldest_message_timestamp_ms": 0,
                },
            },
        )

    snapshot = await CloudflareStatsReader(api(handler), ROUTES, "recovery-id").snapshot()
    assert snapshot.available is False
    assert snapshot.error == "CloudflareQueueError: queue statistics unavailable"
    assert snapshot.backlog_messages.value is None


async def test_provisioner_configures_routes_recovery_and_purge() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/recovery-id"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {"queue_id": "recovery-id", "queue_name": "catalogue-recovery"},
                },
            )
        if request.url.path.endswith("/consumers") and request.method == "GET":
            return httpx.Response(200, json={"success": True, "result": []})
        return httpx.Response(200, json={"success": True, "result": {}})

    provisioner = CloudflareProvisioner(
        api(handler),
        ROUTES,
        "recovery-id",
        visibility_seconds=4200,
        max_retries=100,
    )
    await provisioner.apply()
    await provisioner.purge()

    creates = [
        request
        for request in requests
        if request.method == "POST" and request.url.path.endswith("/consumers")
    ]
    assert len(creates) == 5
    route_body = json.loads(creates[0].content)
    assert route_body["type"] == "http_pull"
    assert route_body["dead_letter_queue"] == "catalogue-recovery"
    assert route_body["settings"]["visibility_timeout_ms"] == 4_200_000
    recovery_body = json.loads(creates[-1].content)
    assert "dead_letter_queue" not in recovery_body

    purges = [request for request in requests if request.url.path.endswith("/purge")]
    assert len(purges) == 5
    assert all(json.loads(request.content)["delete_messages_permanently"] for request in purges)
