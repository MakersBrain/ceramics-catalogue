"""Contract tests for the IPRoyal adapter; no test reaches the provider."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from mb_ceramics_catalogue.providers.base import ProviderError
from mb_ceramics_catalogue.providers.iproyal import IPRoyalProvider


def provider(handler, *, traffic_writes="unconfirmed"):
    return IPRoyalProvider(
        "secret-api-token", traffic_writes=traffic_writes,
        transport=httpx.MockTransport(handler), base_url="https://provider.test",
    )


async def test_health_reads_the_account_and_sends_a_bearer_token():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/residential/me"
        assert request.headers["authorization"] == "Bearer secret-api-token"
        return httpx.Response(200, json={
            "available_traffic": 2.5, "subusers_count": 3, "residential_user_hash": "acct-hash",
        })

    assert await provider(handler).health() is True


async def test_subscription_refuses_rather_than_inventing_a_billing_window():
    """The dates become cycle_start/cycle_end, and cycle_start is a conflict key.

    A synthesised window would be a fabricated billing period under a fabricated
    primary key, so the adapter must fail closed instead.
    """
    p = provider(lambda _: httpx.Response(200, json={"available_traffic": 1}))
    with pytest.raises(ProviderError, match="prepaid balance with no validity window"):
        await p.subscription()


async def test_account_balance_converts_gb_without_binary_float_drift():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"available_traffic": 0.1, "residential_user_hash": "h"})

    # 0.1 GB is exactly 100_000_000 bytes. Through a binary float it is not.
    assert await provider(handler).account_balance_bytes() == 100_000_000


async def test_usage_requests_bytes_so_the_provider_never_rounds():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/residential/me":
            return httpx.Response(200, json={"residential_user_hash": "acct-hash"})
        assert request.url.path == "/residential/data-usage-report"
        seen.update(dict(request.url.params))
        return httpx.Response(200, text="date,traffic\n2026-08-01,1500\n2026-08-02,2500\n")

    report = await provider(handler).usage(
        datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 2, tzinfo=UTC)
    )
    assert seen["hash"] == "acct-hash"
    assert seen["measurement_unit"] == "B"
    assert seen["rounding_decimal"] == "0"
    assert report.total_bytes == 4000
    assert [b.key for b in report.buckets] == ["2026-08-01", "2026-08-02"]


async def test_usage_rejects_groupings_the_report_cannot_express():
    p = provider(lambda _: httpx.Response(200, json={}))
    with pytest.raises(ProviderError, match="by day only"):
        await p.usage(datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 2, tzinfo=UTC),
                      group_by="hour")


async def test_usage_raises_rather_than_silently_dropping_unreadable_rows():
    """Under-reporting spend is the one direction this must never fail in."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/residential/me":
            return httpx.Response(200, json={"residential_user_hash": "h"})
        return httpx.Response(200, text="date,traffic\n2026-08-01,not-a-number\n")

    with pytest.raises(ProviderError, match="non-numeric"):
        await provider(handler).usage(
            datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 2, tzinfo=UTC)
        )


async def test_traffic_writes_fail_closed_until_the_semantics_are_confirmed():
    """A PUT carrying `traffic` may set the balance or add to it; the public
    schema does not say which, and guessing wrong either double-credits or
    wipes a sub-user."""
    p = provider(lambda _: httpx.Response(200, json={}))
    with pytest.raises(ProviderError, match="have not been confirmed"):
        await p.update_subuser("sub-hash", traffic_limit_bytes=100_000_000)


async def test_traffic_writes_are_allowed_once_confirmed():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert json.loads(request.content)["traffic"] == 0.1
        return httpx.Response(200, json={
            "hash": "sub-hash", "username": "catalogue_test",
            "traffic_available": 0.1, "traffic_used": 0,
        })

    result = await provider(handler, traffic_writes="absolute").update_subuser(
        "sub-hash", traffic_limit_bytes=100_000_000
    )
    assert result.id == "sub-hash"
    assert result.traffic_limit_bytes == 100_000_000


async def test_status_updates_are_refused_because_the_resource_has_none():
    p = provider(lambda _: httpx.Response(200, json={}))
    with pytest.raises(ProviderError, match="have no status"):
        await p.update_subuser("sub-hash", status="disabled")


async def test_subuser_uses_hash_not_the_legacy_integer_id():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{
            "id": 42, "hash": "sub-hash", "username": "catalogue_test",
            "traffic_available": 1.5, "traffic_used": 0.25,
        }], "meta": {"last_page": 1}})

    [subuser] = await provider(handler).list_subusers()
    assert subuser.id == "sub-hash"
    assert subuser.status == "unknown"
    assert subuser.traffic_limit_bytes == 1_500_000_000
    assert subuser.traffic_bytes == 250_000_000


async def test_pagination_walks_every_page_and_cannot_run_away():
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 1))
        pages.append(page)
        return httpx.Response(200, json={
            "data": [{
                "hash": f"sub-{page}", "username": f"user{page}",
                "traffic_available": 1, "traffic_used": 0,
            }],
            # A malformed last_page must not spin forever against a paid API.
            "meta": {"last_page": 9999},
        })

    with pytest.raises(ProviderError, match="paginated past 100 pages"):
        await provider(handler).list_subusers()
    assert len(pages) == 100


async def test_a_mutation_is_never_retried():
    """A retried give-traffic is a second grant, so a write gets one attempt."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(503)

    with pytest.raises(ProviderError) as raised:
        await provider(handler).delete_subuser("sub-hash")
    assert calls == ["DELETE"]
    assert raised.value.ambiguous is True


async def test_reads_are_retried_on_a_transient_failure():
    attempts: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"available_traffic": 1, "residential_user_hash": "h"})

    assert await provider(handler).health() is True
    assert len(attempts) == 3


async def test_validation_errors_name_fields_but_never_echo_values():
    """A rejected create echoes the request back, and the request holds a
    sub-user password."""
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"errors": {
            "password": ["The password Hunter2_is_secret is too weak"],
            "username": ["taken"],
        }})

    with pytest.raises(ProviderError) as raised:
        await provider(handler).create_subuser(
            username="catalogue_test", password="Hunter2_is_secret",
            traffic_limit_bytes=100_000_000, traffic_count_from=datetime(2026, 8, 1, tzinfo=UTC),
        )
    assert raised.value.code == "provider_rejected_password_username"
    assert "Hunter2" not in str(raised.value)


async def test_an_oversized_response_is_refused():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="x" * 2_000_001)

    with pytest.raises(ProviderError, match="exceeded 2 MB"):
        await provider(handler).health()
