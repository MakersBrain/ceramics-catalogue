from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from mb_ceramics_catalogue.ops.job_queue import JobEnvelope, NatsJobQueue, durable_for, routes_for
from mb_ceramics_catalogue.ops.outbox import route_for


def test_envelope_round_trip() -> None:
    envelope = JobEnvelope(
        job_id=uuid4(), run_id=uuid4(), source_id="shop", generation=4,
        route="plain.normal", priority=20, enqueued_at=datetime.now(UTC),
    )
    assert JobEnvelope.decode(envelope.encode()) == envelope


@pytest.mark.parametrize(
    ("requires", "requires_any", "selected", "expected"),
    [
        ([], [], None, "plain.normal"),
        (["browser"], [], None, "browser.camoufox.normal"),
        (["browser"], ["browser:camoufox", "browser:cdp_extension_proxy"], None,
         "browser.auto.normal"),
        (["browser"], ["browser:camoufox"], "camoufox", "browser.camoufox.normal"),
    ],
)
def test_route_is_single_and_deterministic(requires, requires_any, selected, expected) -> None:
    assert route_for(requires, requires_any, selected) == expected


def test_worker_routes_are_disjoint_capabilities() -> None:
    assert routes_for([]) == ["plain.normal"]
    assert routes_for(["browser", "browser:camoufox"]) == [
        "browser.camoufox.normal", "browser.auto.normal", "plain.normal"
    ]
    assert durable_for("browser.camoufox.normal") == "catalogue-browser-camoufox-normal"


def test_invalid_schema_and_route_are_rejected() -> None:
    with pytest.raises(ValueError, match="schema"):
        JobEnvelope.decode(b'{"schema":"wrong"}')
    with pytest.raises(ValueError, match="unsupported"):
        durable_for("browser.anything.normal")


@pytest.mark.nats
@pytest.mark.skipif(
    not os.environ.get("CATALOGUE_TEST_NATS_URL"),
    reason="set CATALOGUE_TEST_NATS_URL to a disposable JetStream server",
)
async def test_publish_pull_and_ack_against_jetstream() -> None:
    queue = NatsJobQueue(
        os.environ["CATALOGUE_TEST_NATS_URL"],
        token=os.environ.get("CATALOGUE_TEST_NATS_TOKEN", ""),
    )
    envelope = JobEnvelope(
        job_id=uuid4(), run_id=uuid4(), source_id="e2e", generation=1,
        route="plain.normal", priority=100, enqueued_at=datetime.now(UTC),
    )
    try:
        await queue.connect()
        await queue.publish(envelope)
        delivery = await queue.next_delivery(["plain.normal"])
        assert delivery is not None
        assert delivery.envelope == envelope
        await delivery.ack()
    finally:
        await queue.close()
