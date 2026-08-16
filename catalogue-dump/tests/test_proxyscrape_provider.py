"""Contract tests for the ProxyScrape adapter; no test reaches the provider."""

from datetime import UTC, datetime

import httpx
import pytest

from mb_ceramics_catalogue.providers.base import ProviderError
from mb_ceramics_catalogue.providers.proxyscrape import ProxyScrapeProvider

ACCOUNT = "6f1d0a1e-0000-4000-8000-00000000abcd"


def provider(handler, *, sub_account_id=ACCOUNT):
    return ProxyScrapeProvider(
        "secret-api-token", sub_account_id=sub_account_id,
        transport=httpx.MockTransport(handler), base_url="https://provider.test",
    )


async def test_authenticates_with_the_api_token_header():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["api-token"] == "secret-api-token"
        assert request.url.path == f"/v4/account/{ACCOUNT}/residential/overview"
        return httpx.Response(200, json={
            "plan": "residential", "bandwidth_used": 10, "bandwidth_limit": 100, "status": "active",
        })

    assert await provider(handler).health() is True


async def test_a_missing_sub_account_is_refused_before_any_request():
    """Every residential path is scoped to one, so there is nothing to call."""
    def handler(_: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("no request should be made")

    with pytest.raises(ProviderError, match="needs a sub-account id"):
        await provider(handler, sub_account_id="").health()


async def test_subscription_refuses_because_there_is_no_validity_window():
    p = provider(lambda _: httpx.Response(200, json={}))
    with pytest.raises(ProviderError, match="no validity window"):
        await p.subscription()


async def test_usage_refuses_a_window_because_the_counter_is_cumulative():
    """A running total answered to a windowed question would over-report every
    reconciliation after the first."""
    p = provider(lambda _: httpx.Response(200, json={}))
    with pytest.raises(ProviderError, match="cumulative bandwidth with no date range"):
        await p.usage(datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 2, tzinfo=UTC))


async def test_the_cumulative_counters_are_exposed_honestly_outside_the_protocol():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"bandwidth_used": 1_500, "bandwidth_limit": 9_000})

    assert await provider(handler).account_usage_bytes() == (1_500, 9_000)


async def test_a_non_integer_byte_count_is_refused():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"bandwidth_used": "1500", "bandwidth_limit": 9_000})

    with pytest.raises(ProviderError, match="no usable bandwidth_used"):
        await provider(handler).account_usage_bytes()


async def test_provisioning_is_refused_because_no_ceiling_can_be_set():
    p = provider(lambda _: httpx.Response(200, json={}))
    with pytest.raises(ProviderError, match="carry no traffic ceiling"):
        await p.create_subuser(
            username="catalogue_test", password="Long_password_123",
            traffic_limit_bytes=100_000_000, traffic_count_from=datetime(2026, 8, 1, tzinfo=UTC),
        )


async def test_password_rotation_is_supported_which_is_what_sets_it_apart():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert b"Another_password_123" in request.content
        return httpx.Response(200, json={
            "id": "sub-1", "username": "catalogue_test", "created_at": "2026-08-01T00:00:00Z",
        })

    result = await provider(handler).update_subuser("sub-1", password="Another_password_123")
    assert result.id == "sub-1"
    assert result.username == "catalogue_test"


async def test_setting_a_traffic_limit_on_update_is_refused_too():
    p = provider(lambda _: httpx.Response(200, json={}))
    with pytest.raises(ProviderError, match="no settable traffic ceiling"):
        await p.update_subuser("sub-1", traffic_limit_bytes=100_000_000)


async def test_listing_does_not_fan_out_a_usage_call_per_subuser():
    """Usage lives behind a per-sub-user endpoint; fetching it here would turn
    one request into one-per-sub-user against a paid API."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"data": [
            {"id": "sub-1", "username": "one", "created_at": "2026-08-01T00:00:00Z"},
            {"id": "sub-2", "username": "two", "created_at": "2026-08-01T00:00:00Z"},
        ]})

    subusers = await provider(handler).list_subusers()
    assert [s.id for s in subusers] == ["sub-1", "sub-2"]
    assert all(s.traffic_bytes is None for s in subusers)
    assert len(calls) == 1


async def test_a_mutation_is_never_retried():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(503)

    with pytest.raises(ProviderError) as raised:
        await provider(handler).delete_subuser("sub-1")
    assert calls == ["DELETE"]
    assert raised.value.ambiguous is True
