"""ProxyScrape API v4 adapter.

The only one of the four whose sub-users can be created with a username and a
password this system chooses, and the only one that can rotate that password
afterwards. That makes it the closest of the cheap providers to the credential
lifecycle the profile machinery expects.

It refuses three things, each because the API has no equivalent rather than
because the mapping is awkward:

  * `subscription()` -- `/residential/overview` reports plan, status and
    bandwidth, and no dates at all. Those dates become `cycle_start`/`cycle_end`
    and `(provider, cycle_start)` is the cycle conflict key, so a synthesised
    window would be a fabricated billing period under a fabricated primary key.
    Same refusal, same reason, as IPRoyal.

  * `usage(start, end)` -- every usage endpoint reports a cumulative counter,
    not a window. Returning the cumulative figure in answer to a windowed
    question would be read as "traffic in this period" and would over-report
    every reconciliation after the first. `account_usage_bytes()` and
    `subuser_usage_bytes()` expose the counters honestly, outside the Protocol.

  * `create_subuser()` -- creation takes a username and password and nothing
    else. There is no per-sub-user traffic ceiling to set, here or in the update
    endpoint, so a sub-user can spend the whole account balance. The budget
    model treats the provider-side limit as the backstop under its own
    accounting; without one the application ledger would be the only thing
    between a bug and the balance. Refused, and the registry says
    `can_provision_subusers=False`.

Everything it does report is already in bytes, so nothing on the reconciliation
path needs a unit conversion.

The account is addressed by a sub-account UUID that appears in every residential
path, so it is required at construction rather than discovered per call.

Sources, checked 2026-08-16:
  https://docs.proxyscrape.com/api-overview
  https://docs.proxyscrape.com/llms.txt
  https://docs.proxyscrape.com/api-reference/residential/residential-overview.md
  https://docs.proxyscrape.com/api-reference/residential/create-subuser.md
  https://docs.proxyscrape.com/api-reference/residential/get-subusers.md
  https://docs.proxyscrape.com/api-reference/residential/subuser-usage.md
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import httpx

from .base import ProviderError, Subscription, SubUser, UsageReport


class ProxyScrapeProvider:
    def __init__(
        self,
        api_token: str,
        *,
        sub_account_id: str = "",
        base_url: str = "https://api.proxyscrape.com",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30,
    ) -> None:
        if not api_token:
            raise ProviderError("missing_api_key", "ProxyScrape API token is not configured")
        self.api_token = api_token
        self.sub_account_id = sub_account_id
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.timeout = timeout

    @property
    def _account(self) -> str:
        if not self.sub_account_id:
            raise ProviderError(
                "provider_sub_account_missing",
                "ProxyScrape needs a sub-account id; every residential path is scoped to one",
            )
        return self.sub_account_id

    async def _request(
        self, method: str, path: str, *, json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None, mutation: bool = False,
    ) -> Any:
        response: httpx.Response | None = None
        last_network_error: httpx.TimeoutException | httpx.NetworkError | None = None
        attempts = 1 if mutation else 3
        async with httpx.AsyncClient(
            transport=self.transport, timeout=self.timeout, follow_redirects=False,
        ) as client:
            for attempt in range(attempts):
                try:
                    response = await client.request(
                        method,
                        f"{self.base_url}{path}",
                        # A bespoke header, not Authorization: this is the scheme
                        # the v4 account endpoints document.
                        headers={"api-token": self.api_token, "accept": "application/json"},
                        json=json,
                        params=params,
                    )
                except (httpx.TimeoutException, httpx.NetworkError) as error:
                    last_network_error = error
                    if attempt + 1 < attempts:
                        await asyncio.sleep(0.1 * (2 ** attempt))
                        continue
                    break
                if response.status_code in {429, 500, 502, 503, 504} and attempt + 1 < attempts:
                    await asyncio.sleep(0.1 * (2 ** attempt))
                    continue
                break
        if response is None:
            assert last_network_error is not None
            raise ProviderError(
                "provider_timeout" if isinstance(last_network_error, httpx.TimeoutException)
                else "provider_network",
                "ProxyScrape did not return a conclusive response", ambiguous=mutation,
            ) from last_network_error
        if response.is_redirect:
            raise ProviderError("provider_redirect", "ProxyScrape returned an unexpected redirect")
        if response.status_code in (401, 403):
            raise ProviderError("provider_auth", "ProxyScrape rejected the API token")
        if response.status_code == 429:
            raise ProviderError("provider_rate_limited", "ProxyScrape rate-limited the request")
        if response.status_code >= 500:
            raise ProviderError(
                "provider_unavailable", "ProxyScrape is temporarily unavailable", ambiguous=mutation
            )
        if response.status_code >= 400:
            code = "provider_not_found" if response.status_code == 404 else "provider_rejected"
            raise ProviderError(code, "ProxyScrape rejected the request")
        if response.status_code == 204 or not response.content:
            return {}
        if len(response.content) > 2_000_000:
            raise ProviderError("provider_response_too_large", "ProxyScrape response exceeded 2 MB")
        try:
            return response.json()
        except ValueError as error:
            raise ProviderError(
                "provider_invalid_json", "ProxyScrape returned invalid JSON"
            ) from error

    @staticmethod
    def _bytes(payload: Any, field: str) -> int:
        value = payload.get(field) if isinstance(payload, dict) else None
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ProviderError(
                "provider_usage_unreadable", f"ProxyScrape returned no usable {field}"
            )
        return value

    async def health(self) -> bool:
        await self._request("GET", f"/v4/account/{self._account}/residential/overview")
        return True

    async def account_usage_bytes(self) -> tuple[int, int]:
        """Cumulative `(used, limit)` for the sub-account, in bytes.

        Outside the Protocol because it is a running total with no window, which
        is the whole reason `usage()` refuses.
        """
        payload = await self._request(
            "GET", f"/v4/account/{self._account}/residential/overview"
        )
        return self._bytes(payload, "bandwidth_used"), self._bytes(payload, "bandwidth_limit")

    async def subuser_usage_bytes(self, resource_id: str) -> tuple[int, int]:
        """Cumulative `(used, limit)` for one sub-user, in bytes."""
        payload = await self._request(
            "GET",
            f"/v4/account/{self._account}/residential/subuser/{resource_id}/usage",
        )
        return self._bytes(payload, "bandwidth_used"), self._bytes(payload, "bandwidth_limit")

    async def subscription(self) -> Subscription:
        raise ProviderError(
            "provider_subscription_undated",
            "ProxyScrape reports a plan and a bandwidth allowance but no validity "
            "window, so a cycle cannot be proposed from it; open the cycle with "
            "explicit dates",
        )

    async def usage(self, start: datetime, end: datetime, *, group_by: str = "day") -> UsageReport:
        raise ProviderError(
            "provider_usage_window_unsupported",
            "ProxyScrape reports cumulative bandwidth with no date range; a running "
            "total answered to a windowed question would over-report every "
            "reconciliation after the first",
        )

    async def list_subusers(self) -> list[SubUser]:
        """Traffic is deliberately absent from the listing.

        The list endpoint carries only id, username and created_at; usage lives
        behind a per-sub-user call. Fetching it here would turn one request into
        one-per-sub-user against a paid API on every reconciliation, so callers
        that want a figure ask `subuser_usage_bytes` for the one they care about.
        """
        payload = await self._request(
            "GET", f"/v4/account/{self._account}/residential/subuser/get"
        )
        rows = payload.get("data") if isinstance(payload, dict) and "data" in payload else payload
        if not isinstance(rows, list):
            raise ProviderError("provider_subusers_missing", "ProxyScrape returned no sub-user list")
        return [self._subuser(row) for row in rows]

    @staticmethod
    def _subuser(row: Any) -> SubUser:
        if not isinstance(row, dict):
            raise ProviderError(
                "provider_subuser_invalid", "ProxyScrape returned a malformed sub-user"
            )
        identifier = row.get("id")
        username = row.get("username")
        if not isinstance(identifier, str) or not isinstance(username, str):
            raise ProviderError(
                "provider_subuser_invalid", "ProxyScrape sub-user lacked an id or username"
            )
        return SubUser(
            id=identifier,
            username=username,
            # No status field exists; "unknown" rather than reading enabled into
            # its absence.
            status="unknown",
            traffic_bytes=None,
            traffic_limit_bytes=None,
            auto_disable=False,
        )

    async def create_subuser(
        self, *, username: str, password: str, traffic_limit_bytes: int,
        traffic_count_from: datetime,
    ) -> SubUser:
        raise ProviderError(
            "provider_no_subuser_limit",
            "ProxyScrape sub-users carry no traffic ceiling -- creation takes only a "
            "username and password -- so a provisioned sub-user could spend the whole "
            "account balance with only the application ledger in the way",
        )

    async def update_subuser(
        self, resource_id: str, *, password: str | None = None,
        traffic_limit_bytes: int | None = None, status: str | None = None,
    ) -> SubUser:
        if status is not None:
            raise ProviderError(
                "provider_status_unsupported", "ProxyScrape sub-users have no status field"
            )
        if traffic_limit_bytes is not None:
            raise ProviderError(
                "provider_no_subuser_limit",
                "ProxyScrape sub-users carry no settable traffic ceiling",
            )
        if password is None:
            raise ProviderError("provider_empty_update", "no sub-user fields were supplied")
        payload = await self._request(
            "PUT",
            f"/v4/account/{self._account}/residential/subuser/{resource_id}",
            json={"password": password},
            mutation=True,
        )
        row = payload.get("data") if isinstance(payload, dict) and "data" in payload else payload
        # An update that answers 204 has nothing to parse; report what was known.
        if not row:
            return SubUser(id=resource_id, username=resource_id, status="unknown")
        return self._subuser(row)

    async def delete_subuser(self, resource_id: str) -> None:
        await self._request(
            "DELETE",
            f"/v4/account/{self._account}/residential/subuser/{resource_id}",
            mutation=True,
        )
