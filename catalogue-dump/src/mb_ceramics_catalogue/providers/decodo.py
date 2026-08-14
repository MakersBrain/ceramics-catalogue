"""Decodo Residential public API adapter.

The statistics API reports bytes. The sub-user and subscription APIs expose a
bare numeric traffic limit whose unit is absent from the public schema. Provider
writes therefore remain disabled until deployment explicitly confirms decimal
GB after comparing the live subscription with the dashboard.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import httpx

from .base import ProviderError, Subscription, SubUser, UsageBucket, UsageReport

DECIMAL_GB = 1_000_000_000
LimitUnit = Literal["unconfirmed", "decimal_gb"]


class DecodoProvider:
    def __init__(
        self,
        api_key: str,
        *,
        limit_unit: LimitUnit = "unconfirmed",
        base_url: str = "https://api.decodo.com",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30,
    ) -> None:
        if not api_key:
            raise ProviderError("missing_api_key", "Decodo API key is not configured")
        self.api_key = api_key
        self.limit_unit = limit_unit
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.timeout = timeout

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
                        headers={"authorization": self.api_key, "accept": "application/json"},
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
                "Decodo did not return a conclusive response", ambiguous=mutation,
            ) from last_network_error
        if response.is_redirect:
            raise ProviderError("provider_redirect", "Decodo returned an unexpected redirect")
        if response.status_code in (401, 403):
            raise ProviderError("provider_auth", "Decodo rejected the API key")
        if response.status_code == 429:
            raise ProviderError("provider_rate_limited", "Decodo rate-limited the request")
        if response.status_code >= 500:
            raise ProviderError(
                "provider_unavailable", "Decodo is temporarily unavailable", ambiguous=mutation
            )
        if response.status_code >= 400:
            code = "provider_not_found" if response.status_code == 404 else "provider_rejected"
            try:
                payload = response.json()
                if isinstance(payload, dict) and isinstance(payload.get("error_code"), str):
                    code = f"provider_{payload['error_code']}"
            except ValueError:
                pass
            raise ProviderError(code, "Decodo rejected the request")
        if response.status_code == 204 or not response.content:
            return {}
        if len(response.content) > 2_000_000:
            raise ProviderError("provider_response_too_large", "Decodo response exceeded 2 MB")
        try:
            return response.json()
        except ValueError as error:
            raise ProviderError("provider_invalid_json", "Decodo returned invalid JSON") from error

    def _limit_to_bytes(self, value: Any) -> int | None:
        if value is None:
            return None
        if self.limit_unit != "decimal_gb":
            return None
        try:
            return int(Decimal(str(value)) * DECIMAL_GB)
        except (InvalidOperation, ValueError) as error:
            raise ProviderError("provider_invalid_limit", "Decodo returned an invalid limit") from error

    def _bytes_to_limit(self, value: int) -> float:
        if self.limit_unit != "decimal_gb":
            raise ProviderError(
                "provider_limit_unit_unconfirmed",
                "Decodo traffic-limit units have not been confirmed against the live subscription",
            )
        if value < 0 or value % 1_000_000 != 0:
            raise ProviderError("provider_invalid_limit", "traffic limit must use whole decimal MB")
        return float(Decimal(value) / DECIMAL_GB)

    @staticmethod
    def _date(value: Any, *, end: bool = False) -> datetime:
        if not isinstance(value, str):
            raise ProviderError("provider_invalid_date", "Decodo subscription omitted its dates")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        # Public subscription examples use inclusive date-only validity. Treat
        # valid_until as the exclusive start of the following UTC day.
        if end and len(value) == 10:
            from datetime import timedelta

            parsed += timedelta(days=1)
        return parsed.astimezone(UTC)

    async def health(self) -> bool:
        await self.subscription()
        return True

    async def subscription(self) -> Subscription:
        payload = await self._request("GET", "/v2/subscriptions")
        if isinstance(payload, list):
            candidates = [row for row in payload if row.get("service_type") == "residential_proxies"]
            if len(candidates) != 1:
                raise ProviderError("provider_subscription_ambiguous", "expected one residential subscription")
            payload = candidates[0]
        if not isinstance(payload, dict) or payload.get("service_type") != "residential_proxies":
            raise ProviderError("provider_subscription_missing", "no Residential subscription was returned")
        raw_limit = payload.get("traffic_limit")
        return Subscription(
            provider_resource_id=str(payload.get("id")) if payload.get("id") is not None else None,
            service_type="residential_proxies",
            traffic_limit_bytes=self._limit_to_bytes(raw_limit),
            raw_traffic_limit=raw_limit,
            valid_from=self._date(payload.get("valid_from")),
            valid_until=self._date(payload.get("valid_until"), end=True),
            users_limit=payload.get("users_limit"),
        )

    async def usage(self, start: datetime, end: datetime, *, group_by: str = "day") -> UsageReport:
        if group_by not in {"hour", "day", "week", "month", "target"}:
            raise ProviderError("invalid_group", "unsupported Decodo traffic grouping")
        fmt = "%Y-%m-%d %H:%M:%S"
        request_body = {
                "proxyType": "residential_proxies",
                "startDate": start.astimezone(UTC).strftime(fmt),
                "endDate": min(end, datetime.now(UTC)).astimezone(UTC).strftime(fmt),
                "groupBy": group_by,
                "limit": 500,
                "sortBy": "grouping_key",
                "sortOrder": "asc",
        }
        payload = await self._request(
            "POST", "/api/v2/statistics/traffic", json={**request_body, "page": 1}
        )
        try:
            metadata = payload["metadata"]
            total_pages = int(metadata.get("total_pages", 1))
            if total_pages < 1 or total_pages > 20:
                raise ProviderError(
                    "provider_pagination",
                    "Decodo usage exceeded the 20-page reconciliation bound",
                )
            totals = metadata["totals"]
            rows = list(payload.get("data", []))
            for page in range(2, total_pages + 1):
                next_payload = await self._request(
                    "POST", "/api/v2/statistics/traffic",
                    json={**request_body, "page": page},
                )
                next_metadata = next_payload["metadata"]
                if int(next_metadata.get("total_pages", total_pages)) != total_pages:
                    raise ProviderError(
                        "provider_pagination_changed",
                        "Decodo usage pagination changed during reconciliation",
                    )
                rows.extend(next_payload.get("data", []))
            buckets = [
                UsageBucket(
                    key=str(row["key"]),
                    transmitted_bytes=max(0, int(row.get("tx_bytes", 0))),
                    received_bytes=max(0, int(row.get("rx_bytes", 0))),
                    total_bytes=max(0, int(row.get("rx_tx_bytes", row.get("total_rx_tx", 0)))),
                    requests=max(0, int(row.get("requests", 0))),
                )
                for row in rows
            ]
            return UsageReport(
                total_transmitted_bytes=max(0, int(totals["total_tx"])),
                total_received_bytes=max(0, int(totals["total_rx"])),
                total_bytes=max(0, int(totals["total_rx_tx"])),
                requests=max(0, int(totals.get("requests", 0))),
                buckets=buckets,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ProviderError("provider_invalid_usage", "Decodo usage response was incomplete") from error

    def _subuser(self, row: Any) -> SubUser:
        if not isinstance(row, dict):
            raise ProviderError("provider_invalid_subuser", "Decodo returned an invalid sub-user")
        return SubUser(
            id=str(row["id"]),
            username=str(row["username"]),
            status=str(row.get("status", "active")),
            traffic_bytes=self._limit_to_bytes(row.get("traffic")),
            traffic_limit_bytes=self._limit_to_bytes(row.get("traffic_limit")),
            auto_disable=bool(row.get("auto_disable", False)),
            traffic_count_from=self._date(row["traffic_count_from"])
            if row.get("traffic_count_from") else None,
        )

    async def list_subusers(self) -> list[SubUser]:
        payload = await self._request(
            "GET", "/v2/sub-users", params={"service_type": "residential_proxies"}
        )
        if not isinstance(payload, list):
            raise ProviderError("provider_invalid_subusers", "Decodo returned an invalid sub-user list")
        return [self._subuser(row) for row in payload]

    async def _find_subuser(self, resource_id: str) -> SubUser:
        for user in await self.list_subusers():
            if user.id == resource_id:
                return user
        raise ProviderError("provider_not_found", "Decodo sub-user was not found")

    async def create_subuser(
        self, *, username: str, password: str, traffic_limit_bytes: int,
        traffic_count_from: datetime,
    ) -> SubUser:
        await self._request(
            "POST",
            "/v2/sub-users",
            mutation=True,
            json={
                "username": username,
                "password": password,
                "service_type": "residential_proxies",
                "traffic_limit": self._bytes_to_limit(traffic_limit_bytes),
                "auto_disable": True,
                "traffic_count_from": traffic_count_from.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        matches = [user for user in await self.list_subusers() if user.username == username]
        if len(matches) != 1:
            raise ProviderError(
                "provider_create_unconfirmed", "Decodo accepted creation but it could not be confirmed",
                ambiguous=True,
            )
        return matches[0]

    async def update_subuser(
        self, resource_id: str, *, password: str | None = None,
        traffic_limit_bytes: int | None = None, status: str | None = None,
    ) -> SubUser:
        body: dict[str, Any] = {}
        if password is not None:
            body["password"] = password
        if traffic_limit_bytes is not None:
            body["traffic_limit"] = self._bytes_to_limit(traffic_limit_bytes)
            body["auto_disable"] = True
        if status is not None:
            if status not in {"active", "disabled"}:
                raise ProviderError("invalid_status", "sub-user status must be active or disabled")
            body["status"] = status
        if not body:
            raise ProviderError("empty_update", "no sub-user change was requested")
        await self._request("PUT", f"/v2/sub-users/{resource_id}", json=body, mutation=True)
        if status == "disabled":
            return SubUser(id=resource_id, username="", status="disabled")
        return await self._find_subuser(resource_id)

    async def delete_subuser(self, resource_id: str) -> None:
        try:
            await self._request("DELETE", f"/v2/sub-users/{resource_id}", mutation=True)
        except ProviderError as error:
            if error.code != "provider_not_found":
                raise
