from __future__ import annotations

import httpx
from mb_ceramics_catalogue.observability import metrics

from catalogue_service.app import create_app


async def test_metrics_route_and_request_correlation_work_without_database_access() -> None:
    metrics.REGISTRY.clear()
    app = create_app("postgresql:///unused")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://service") as client:
        failed = await client.get(
            "/v1/canonical-products/not-a-uuid", headers={"x-request-id": "service-test"}
        )
        scrape = await client.get("/metrics")

    assert failed.status_code == 400
    assert failed.headers["x-request-id"] == "service-test"
    assert scrape.status_code == 200
    assert 'route="/v1/canonical-products/{id}"' in scrape.text
    assert "not-a-uuid" not in scrape.text
    assert 'status_class="4xx"' in scrape.text

    # Starlette's ServerErrorMiddleware sits outside user middleware. Exercise
    # that exact stack rather than only calling RequestTelemetry in isolation.
    crash_transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=crash_transport, base_url="http://service") as client:
        crashed = await client.get("/health", headers={"x-request-id": "service-crash"})
    assert crashed.status_code == 500
    assert crashed.headers["x-request-id"] == "service-crash"
