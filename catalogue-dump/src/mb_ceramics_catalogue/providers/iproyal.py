"""IPRoyal Residential public API adapter.

IPRoyal sells a prepaid traffic balance rather than a time-boxed subscription,
and that single difference is what shapes this file. Three of the seven
`ProxyProvider` methods cannot mean here what they mean for Decodo, and each one
refuses explicitly rather than inventing a plausible answer:

  * `subscription()` raises. `GET /residential/me` reports a balance and nothing
    else - no `valid_from`, no `valid_until`. Those two fields are not
    decoration: `propose_cycle` writes them straight into `cycle_start` and
    `cycle_end`, and `(provider, cycle_start)` is the conflict key for the whole
    budget cycle. A synthesised window would be a fabricated billing period
    carrying a fabricated primary key. An IPRoyal cycle has to be opened by an
    operator who states the dates.

  * `update_subuser(status=...)` raises. The Subuser resource has no status
    field; there is nothing to set.

  * `update_subuser(traffic_limit_bytes=...)` raises until the write semantics
    are confirmed - see `traffic_writes` below.

Units are the other half of the story. The sub-user and account APIs speak GB as
floats, which is the wrong shape for money: a budget that reserves bytes cannot
be reconciled against a number that has already been rounded. So every read is
converted through `Decimal`, never binary float, and the usage report is
requested in bytes (`measurement_unit=B`, `rounding_decimal=0`) so no rounding
happens on the provider's side at all.

Sources, checked 2026-08-16:
  https://docs.iproyal.com/proxies/residential/api
  https://docs.iproyal.com/proxies/residential/api/user
  https://docs.iproyal.com/proxies/residential/api/sub-users
"""

from __future__ import annotations

import asyncio
import csv
import io
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import httpx

from .base import ProviderError, Subscription, SubUser, UsageBucket, UsageReport

DECIMAL_GB = 1_000_000_000

# Whether a PUT that carries `traffic` sets the balance or adds to it.
#
# The documented body for an update is "same as create, all optional", and
# create's `traffic` is the opening allocation - so on the face of it a PUT sets
# it. But IPRoyal also ships `give-traffic` and `take-traffic`, which only exist
# if some other call does not already express a delta, and nothing in the public
# schema says which reading is right. Guessing wrong either double-credits a
# sub-user or silently wipes its remaining balance.
#
# This mirrors `DecodoProvider.limit_unit`, and for the same reason: a unit or a
# verb that the public schema leaves ambiguous stays disabled until a deployment
# confirms it against a live account.
TrafficWrites = Literal["unconfirmed", "absolute"]


class IPRoyalProvider:
    def __init__(
        self,
        api_token: str,
        *,
        traffic_writes: TrafficWrites = "unconfirmed",
        base_url: str = "https://resi-api.iproyal.com/v1",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30,
    ) -> None:
        if not api_token:
            raise ProviderError("missing_api_key", "IPRoyal API token is not configured")
        self.api_token = api_token
        self.traffic_writes = traffic_writes
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.timeout = timeout

    # ---- transport ---------------------------------------------------------

    async def _send(
        self, method: str, path: str, *, json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None, mutation: bool = False, accept: str = "application/json",
    ) -> httpx.Response:
        """Retry idempotent reads, never a mutation.

        A retried mutation on a traffic endpoint is a second grant of traffic, so
        a write gets exactly one attempt and any inconclusive outcome is raised
        as `ambiguous` for the caller to reconcile rather than retry.
        """
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
                        headers={
                            "authorization": f"Bearer {self.api_token}",
                            "accept": accept,
                        },
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
                "IPRoyal did not return a conclusive response", ambiguous=mutation,
            ) from last_network_error
        if response.is_redirect:
            raise ProviderError("provider_redirect", "IPRoyal returned an unexpected redirect")
        if response.status_code in (401, 403):
            raise ProviderError("provider_auth", "IPRoyal rejected the API token")
        if response.status_code == 429:
            raise ProviderError("provider_rate_limited", "IPRoyal rate-limited the request")
        if response.status_code >= 500:
            raise ProviderError(
                "provider_unavailable", "IPRoyal is temporarily unavailable", ambiguous=mutation
            )
        if response.status_code >= 400:
            raise ProviderError(self._rejection_code(response), "IPRoyal rejected the request")
        if len(response.content) > 2_000_000:
            raise ProviderError("provider_response_too_large", "IPRoyal response exceeded 2 MB")
        return response

    @staticmethod
    def _rejection_code(response: httpx.Response) -> str:
        """Name the rejected fields, never their values.

        A validation error echoes back what was sent, and what was sent to these
        endpoints includes sub-user passwords. Only field *names* that look like
        identifiers are carried into the error code; everything else is dropped.
        """
        code = "provider_not_found" if response.status_code == 404 else "provider_rejected"
        try:
            payload = response.json()
        except ValueError:
            return code
        if not isinstance(payload, dict):
            return code
        errors = payload.get("errors")
        if isinstance(errors, dict):
            fields = sorted(
                key for key in errors
                if isinstance(key, str) and key.replace("_", "").isalnum()
            )
            if fields:
                return f"{code}_{'_'.join(fields)}"
        return code

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._send(method, path, **kwargs)
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError as error:
            raise ProviderError("provider_invalid_json", "IPRoyal returned invalid JSON") from error

    # ---- units -------------------------------------------------------------

    @staticmethod
    def _gb_to_bytes(value: Any) -> int | None:
        """GB (as documented) to bytes, through Decimal.

        `Decimal(str(value))` and not `Decimal(value)`: the API sends JSON
        floats, and going through the binary float would turn 0.1 GB into
        99999999.99999999 bytes before it ever reached the ledger.
        """
        if value is None:
            return None
        try:
            return int(Decimal(str(value)) * DECIMAL_GB)
        except (InvalidOperation, ValueError) as error:
            raise ProviderError(
                "provider_invalid_traffic", "IPRoyal returned an unreadable traffic value"
            ) from error

    @staticmethod
    def _bytes_to_gb(value: int) -> float:
        if value < 0 or value % 1_000_000 != 0:
            raise ProviderError("provider_invalid_limit", "traffic must use whole decimal MB")
        return float(Decimal(value) / DECIMAL_GB)

    # ---- protocol ----------------------------------------------------------

    async def health(self) -> bool:
        """Deliberately not `await self.subscription()`.

        The Decodo adapter can define health as "the subscription reads", because
        it has one. Here `subscription()` always raises by design, so health has
        to be the cheapest call that proves the token works: the account read.
        """
        await self._json("GET", "/residential/me")
        return True

    async def account_balance_bytes(self) -> int:
        """The prepaid balance, which is the nearest thing to a subscription.

        Not part of `ProxyProvider`. It is exposed because it is the number an
        operator needs to type a cycle's `purchased_bytes` by hand, which is the
        only way an IPRoyal cycle can be opened -- see `subscription()`.
        """
        payload = await self._json("GET", "/residential/me")
        if not isinstance(payload, dict):
            raise ProviderError("provider_account_missing", "IPRoyal returned no account")
        balance = self._gb_to_bytes(payload.get("available_traffic"))
        if balance is None:
            raise ProviderError("provider_account_missing", "IPRoyal omitted the available traffic")
        return balance

    async def subscription(self) -> Subscription:
        raise ProviderError(
            "provider_subscription_unbounded",
            "IPRoyal sells a prepaid balance with no validity window, so a cycle "
            "cannot be proposed from it; open the cycle with explicit dates",
        )

    async def usage(self, start: datetime, end: datetime, *, group_by: str = "day") -> UsageReport:
        """The data-usage report, which is a CSV download rather than JSON.

        Only daily grouping exists: the endpoint buckets by date and offers no
        other dimension, so the hour/week/month/target groupings the Decodo
        adapter accepts are rejected here instead of being silently rounded to
        something else.

        Requested in bytes with zero decimal places so the provider does no
        rounding of its own. Asking in GB would hand back a figure already
        rounded to the requested precision, and reconciling a byte ledger
        against that is how a discrepancy becomes permanent.
        """
        if group_by != "day":
            raise ProviderError("invalid_group", "IPRoyal reports traffic by day only")
        account = await self._json("GET", "/residential/me")
        user_hash = account.get("residential_user_hash") if isinstance(account, dict) else None
        if not isinstance(user_hash, str) or not user_hash:
            raise ProviderError("provider_account_missing", "IPRoyal omitted the account hash")

        response = await self._send(
            "GET",
            "/residential/data-usage-report",
            params={
                "hash": user_hash,
                "date_from": start.astimezone(UTC).strftime("%Y-%m-%d"),
                "date_to": min(end, datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%d"),
                "measurement_unit": "B",
                "rounding_decimal": 0,
                "time_zone": "UTC",
            },
            accept="text/csv",
        )
        return self._parse_usage_csv(response.text)

    @staticmethod
    def _parse_usage_csv(body: str) -> UsageReport:
        """Read the report defensively: column order is not part of any contract.

        Columns are matched by normalised header name rather than position, and
        an unreadable row raises instead of being skipped -- a usage report that
        quietly drops rows under-reports spend, which is the one direction this
        must never fail in.
        """
        rows = list(csv.DictReader(io.StringIO(body)))
        buckets: list[UsageBucket] = []
        total = 0
        for row in rows:
            normalised = {
                (key or "").strip().lower().replace(" ", "_"): (value or "").strip()
                for key, value in row.items()
            }
            key = normalised.get("date") or normalised.get("day")
            raw = (
                normalised.get("traffic")
                or normalised.get("usage")
                or normalised.get("bytes")
                or normalised.get("total")
            )
            if key is None or raw is None:
                raise ProviderError(
                    "provider_usage_unreadable", "IPRoyal usage report had unexpected columns"
                )
            if raw == "":
                continue
            try:
                used = int(Decimal(raw))
            except (InvalidOperation, ValueError) as error:
                raise ProviderError(
                    "provider_usage_unreadable", "IPRoyal usage report had a non-numeric total"
                ) from error
            if used < 0:
                raise ProviderError(
                    "provider_usage_unreadable", "IPRoyal usage report had a negative total"
                )
            total += used
            # The report gives one figure per day and does not split sent from
            # received. It is recorded as received so the two never double-count;
            # `total_bytes` is the number the budget actually reconciles against.
            buckets.append(UsageBucket(key=key, received_bytes=used, total_bytes=used))
        return UsageReport(
            total_received_bytes=total, total_bytes=total, buckets=buckets,
        )

    async def list_subusers(self) -> list[SubUser]:
        subusers: list[SubUser] = []
        page = 1
        while True:
            payload = await self._json(
                "GET", "/residential-subusers", params={"page": page, "per_page": 100}
            )
            rows = payload.get("data") if isinstance(payload, dict) else payload
            if not isinstance(rows, list):
                raise ProviderError("provider_subusers_missing", "IPRoyal returned no sub-user list")
            subusers.extend(self._subuser(row) for row in rows)
            meta = payload.get("meta") if isinstance(payload, dict) else None
            last = meta.get("last_page") if isinstance(meta, dict) else None
            if not isinstance(last, int) or page >= last:
                break
            page += 1
            # A malformed `last_page` must not spin forever against a paid API.
            if page > 100:
                raise ProviderError("provider_pagination_runaway", "IPRoyal paginated past 100 pages")
        return subusers

    @classmethod
    def _subuser(cls, row: Any) -> SubUser:
        if not isinstance(row, dict):
            raise ProviderError("provider_subuser_invalid", "IPRoyal returned a malformed sub-user")
        # `hash`, never `id`: the docs mark the integer id as legacy.
        resource_id = row.get("hash")
        username = row.get("username")
        if not isinstance(resource_id, str) or not isinstance(username, str):
            raise ProviderError("provider_subuser_invalid", "IPRoyal sub-user lacked hash or username")
        available = cls._gb_to_bytes(row.get("traffic_available"))
        used = cls._gb_to_bytes(row.get("traffic_used"))
        return SubUser(
            id=resource_id,
            username=username,
            # The resource carries no status. "unknown" is the honest value: a
            # caller that needs enabled/disabled must not read one into this.
            status="unknown",
            traffic_bytes=used,
            # Available is a remaining balance, not a ceiling. It is the closest
            # analogue the API offers and is recorded as such.
            traffic_limit_bytes=available,
            auto_disable=False,
        )

    async def create_subuser(
        self, *, username: str, password: str, traffic_limit_bytes: int,
        traffic_count_from: datetime,
    ) -> SubUser:
        """`traffic_count_from` is accepted and ignored, because IPRoyal has no
        equivalent: a sub-user's balance starts when it is created. Silently
        dropping it is safe in a way that silently dropping a limit would not
        be -- it narrows nothing and grants nothing."""
        payload = await self._json(
            "POST", "/residential-subusers",
            json={
                "username": username,
                "password": password,
                "traffic": self._bytes_to_gb(traffic_limit_bytes),
            },
            mutation=True,
        )
        return self._subuser(payload.get("data") if isinstance(payload, dict) and "data" in payload else payload)

    async def update_subuser(
        self, resource_id: str, *, password: str | None = None,
        traffic_limit_bytes: int | None = None, status: str | None = None,
    ) -> SubUser:
        if status is not None:
            raise ProviderError(
                "provider_status_unsupported",
                "IPRoyal sub-users have no status; delete the sub-user or remove its traffic",
            )
        body: dict[str, Any] = {}
        if password is not None:
            body["password"] = password
        if traffic_limit_bytes is not None:
            if self.traffic_writes != "absolute":
                raise ProviderError(
                    "provider_traffic_writes_unconfirmed",
                    "IPRoyal traffic-write semantics have not been confirmed against a live "
                    "account; a PUT may set or may add",
                )
            body["traffic"] = self._bytes_to_gb(traffic_limit_bytes)
        if not body:
            raise ProviderError("provider_empty_update", "no sub-user fields were supplied")
        payload = await self._json(
            "PUT", f"/residential-subusers/{resource_id}", json=body, mutation=True
        )
        return self._subuser(payload.get("data") if isinstance(payload, dict) and "data" in payload else payload)

    async def delete_subuser(self, resource_id: str) -> None:
        await self._json("DELETE", f"/residential-subusers/{resource_id}", mutation=True)
