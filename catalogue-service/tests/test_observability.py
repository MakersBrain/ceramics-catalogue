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
