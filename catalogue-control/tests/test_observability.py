from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from mb_ceramics_catalogue.config.sources import SourcesFile
from mb_ceramics_catalogue.observability import metrics
from mb_ceramics_catalogue.ops.delivery import (
    Measurement,
    QueueRouteSnapshot,
    QueueSnapshot,
)

from catalogue_control.app import _publish_queue_metrics, _source_metric_snapshot, create_app
from catalogue_control.settings import Settings


def test_source_snapshot_is_schedule_aware_and_includes_never_successful_sources() -> None:
    sources = SourcesFile.model_validate(
        {
            "late": {"label": "Late", "url": "https://late.test", "scraper": "shopify"},
            "new": {"label": "New", "url": "https://new.test", "scraper": "shopify"},
            "paused": {"label": "Paused", "url": "https://paused.test", "scraper": "shopify"},
        }
    )
    now = datetime.now(UTC)
    history = [
        {
            "source_id": "late",
            "enabled": True,
            "paused": False,
            "schedule_id": None,
            "last_success_at": now - timedelta(days=2),
            "last_records": 50,
            "previous_records": 100,
        },
        {
            "source_id": "paused",
            "enabled": True,
            "paused": True,
            "schedule_id": None,
            "last_success_at": None,
            "last_records": None,
            "previous_records": None,
        },
    ]
    schedules = [
        {
            "id": "daily",
            "enabled": True,
            "cron": "0 3 * * *",
            "timezone": "Europe/Paris",
            "source_filter": {"all": True},
            "last_fired_at": now - timedelta(hours=2),
            "next_fire_at": now + timedelta(hours=22),
        }
    ]

    snapshot = {row["source"]: row for row in _source_metric_snapshot(sources, history, schedules)}

    assert float(snapshot["late"]["overdue"] or 0) > 3500
    assert snapshot["late"]["succeeded"] == 1
    assert snapshot["late"]["record_ratio"] == 0.5
    assert snapshot["new"]["succeeded"] == 0
    assert snapshot["new"]["records"] is None
    assert "paused" not in snapshot


def test_source_snapshot_detects_an_expected_fire_the_scheduler_missed() -> None:
    sources = SourcesFile.model_validate(
        {"late": {"label": "Late", "url": "https://late.test", "scraper": "shopify"}}
    )
    now = datetime.now(UTC)
    snapshot = _source_metric_snapshot(
        sources,
        [
            {
                "source_id": "late",
                "enabled": True,
                "paused": False,
                "schedule_id": None,
                # This satisfied yesterday's occurrence, but not today's missed one.
                "last_success_at": now - timedelta(hours=23),
                "last_records": 10,
                "previous_records": 10,
            }
        ],
        [
            {
                "id": "hourly",
                "enabled": True,
                "cron": "0 * * * *",
                "timezone": "UTC",
                "source_filter": {"all": True},
                "last_fired_at": now - timedelta(days=1),
                "next_fire_at": now - timedelta(hours=2),
            }
        ],
    )

    assert float(snapshot[0]["overdue"] or 0) > 0


def test_queue_metrics_label_accuracy_and_omit_unsupported_fields() -> None:
    metrics.REGISTRY.clear()
    now = datetime.now(UTC)
    snapshot = QueueSnapshot(
        provider="cloudflare",
        observed_at=now,
        last_success_at=now,
        available=True,
        backlog_messages=Measurement.best_effort(3),
        backlog_bytes=Measurement.best_effort(120),
        consumer_count=Measurement.unsupported(),
        routes=(
            QueueRouteSnapshot(
                route="plain.normal",
                ready=Measurement.best_effort(3),
                in_flight=Measurement.unsupported(),
                redelivered=Measurement.unsupported(),
                delivered=Measurement.unsupported(),
                oldest_age_seconds=Measurement.best_effort(4.0),
            ),
        ),
    )

    _publish_queue_metrics(metrics, snapshot, 2.0)
    rendered = metrics.render()
    assert 'catalogue_queue_backlog_messages{accuracy="best_effort",provider="cloudflare"} 3\n' in rendered
    assert (
        'catalogue_queue_route_ready{accuracy="best_effort",provider="cloudflare",route="plain.normal"} 3\n'
        in rendered
    )
    assert "catalogue_queue_consumers{" not in rendered
    assert "catalogue_queue_route_in_flight{" not in rendered


async def test_request_middleware_covers_auth_failures_without_concrete_paths() -> None:
    metrics.REGISTRY.clear()
    app = create_app(Settings(dsn="postgresql:///unused", control_token="test-token"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://control") as client:
        response = await client.get("/v1/jobs/one-concrete-id", headers={"x-request-id": "control-test"})

    assert response.status_code == 401
    assert response.headers["x-request-id"] == "control-test"
    rendered = metrics.render()
    assert 'route="/v1/jobs/{id}"' in rendered
    assert "one-concrete-id" not in rendered
    assert 'status_class="4xx"' in rendered
