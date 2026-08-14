"""Contract tests for the Decodo adapter; no test reaches the provider."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from mb_ceramics_catalogue.providers.base import ProviderError
from mb_ceramics_catalogue.providers.decodo import DecodoProvider


def provider(handler, *, unit="decimal_gb"):
    return DecodoProvider(
        "secret-api-key", limit_unit=unit,
        transport=httpx.MockTransport(handler), base_url="https://provider.test",
    )


async def test_subscription_filters_residential_and_normalizes_decimal_gb():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/subscriptions"
        assert request.headers["authorization"] == "secret-api-key"
        return httpx.Response(200, json=[{
            "id": 17, "service_type": "residential_proxies", "traffic_limit": 3,
            "valid_from": "2026-08-01", "valid_until": "2026-08-31", "users_limit": 5,
        }])

    result = await provider(handler).subscription()
    assert result.traffic_limit_bytes == 3_000_000_000
    assert result.valid_until == datetime(2026, 9, 1, tzinfo=UTC)


async def test_limit_mutation_fails_closed_until_unit_is_confirmed():
    p = provider(lambda _: httpx.Response(500), unit="unconfirmed")
    with pytest.raises(ProviderError, match="units have not been confirmed"):
        await p.create_subuser(
            username="catalogue_test", password="Long_password_123",
            traffic_limit_bytes=100_000_000,
            traffic_count_from=datetime(2026, 8, 1, tzinfo=UTC),
        )


async def test_create_subuser_uses_provider_creation_time_for_counter():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            body = json.loads(request.content)
            assert "traffic_count_from" not in body
            assert body["traffic_limit"] == 0.1
            return httpx.Response(201, json={})
        return httpx.Response(200, json=[{
            "id": "new-user", "username": "catalogue_test", "status": "active",
            "traffic_limit": 0.1, "auto_disable": True,
            "traffic_count_from": "2026-08-14 12:00:00",
        }])

    result = await provider(handler).create_subuser(
        username="catalogue_test", password="Long_password_123+",
        traffic_limit_bytes=100_000_000,
        traffic_count_from=datetime(2026, 8, 1, tzinfo=UTC),
    )
    assert result.id == "new-user"
    assert [request.method for request in requests] == ["POST", "GET"]


async def test_usage_fetches_bounded_pagination_and_uses_first_page_totals():
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        pages.append(body["page"])
        page = body["page"]
        return httpx.Response(200, json={
            "metadata": {
                "total_pages": 2,
                "totals": {"total_tx": 4, "total_rx": 6, "total_rx_tx": 10, "requests": 2},
            },
            "data": [{
                "key": f"2026-08-0{page}T00:00:00Z", "tx_bytes": page,
                "rx_bytes": page + 1, "rx_tx_bytes": page * 2 + 1, "requests": 1,
            }],
        })

    result = await provider(handler).usage(
        datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 3, tzinfo=UTC)
    )
    assert pages == [1, 2]
    assert result.total_bytes == 10
    assert len(result.buckets) == 2


async def test_ambiguous_provider_failure_is_sanitized():
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("contained-secret-api-key")

    with pytest.raises(ProviderError) as caught:
        await provider(handler).delete_subuser("resource-1")
    assert caught.value.code == "provider_timeout"
    assert caught.value.ambiguous is True
    assert attempts == 1
    assert "secret-api-key" not in str(caught.value)


async def test_rejection_preserves_only_safe_validation_field_names():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "error_code": "bad_request",
            "error": {"password": "rejected echoed-secret-value"},
        })

    with pytest.raises(ProviderError) as caught:
        await provider(handler).create_subuser(
            username="catalogue_test", password="Echoed_secret_123+",
            traffic_limit_bytes=100_000_000,
            traffic_count_from=datetime(2026, 8, 1, tzinfo=UTC),
        )
    assert caught.value.code == "provider_bad_request_password"
    assert "echoed-secret-value" not in str(caught.value)


async def test_safe_read_retries_a_transient_failure():
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=[{
            "id": "subscription", "service_type": "residential_proxies", "traffic_limit": 3,
            "valid_from": "2026-08-01", "valid_until": "2026-08-31",
        }])

    assert (await provider(handler).subscription()).traffic_limit_bytes == 3_000_000_000
    assert attempts == 2


async def test_oversized_provider_response_is_rejected():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{" + b" " * 2_000_001 + b"}")

    with pytest.raises(ProviderError) as caught:
        await provider(handler).subscription()
    assert caught.value.code == "provider_response_too_large"
