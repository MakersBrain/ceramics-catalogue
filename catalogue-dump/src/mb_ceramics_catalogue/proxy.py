"""Job-scoped Decodo identities and fail-closed shared budget accounting."""

from __future__ import annotations

import json
import math
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote
from uuid import UUID

import httpx

if TYPE_CHECKING:
    from psycopg import AsyncConnection

DECIMAL_MB = 1_000_000
DEFAULT_JOB_BYTES = 25 * DECIMAL_MB
_USERINFO = re.compile(r"(?P<scheme>https?://)[^/@\s]+@", re.IGNORECASE)


class ProxyDenied(RuntimeError):
    """No new paid traffic may start under the current safety state."""


def redact_url(value: str) -> str:
    """Remove URL userinfo without changing the useful endpoint identity."""
    return _USERINFO.sub(r"\g<scheme>[REDACTED]@", value)


@dataclass(frozen=True)
class ProxyProfile:
    name: str
    host: str
    port: int
    username: str
    password: str
    api_key: str | None = None
    username_template: str = "{username}-country-{country}-session-{session}-sessionduration-{minutes}"

    def username_for(self, country: str | None, session: str, minutes: int) -> str:
        return self.username_template.format(
            username=self.username,
            country=(country or "any").lower(),
            session=session,
            minutes=minutes,
        )


def load_profiles(path: Path) -> dict[str, ProxyProfile]:
    """Read profiles from a mode-0600 mounted JSON secret."""
    stat = path.stat()
    if stat.st_mode & 0o077:
        raise ProxyDenied(f"proxy secret {path} must not be accessible by group or other users")
    raw = json.loads(path.read_text(encoding="utf-8"))
    profiles: dict[str, ProxyProfile] = {}
    for name, value in raw.items():
        if not isinstance(value, dict):
            raise ProxyDenied(f"proxy profile {name!r} is not an object")
        host = str(value.get("host", ""))
        if "://" in host or "@" in host:
            raise ProxyDenied(f"proxy profile {name!r} host must not be a URL")
        profiles[name] = ProxyProfile(
            name=name,
            host=host,
            port=int(value["port"]),
            username=str(value["username"]),
            password=str(value["password"]),
            api_key=str(value["api_key"]) if value.get("api_key") else None,
            username_template=str(value.get("username_template") or ProxyProfile.username_template),
        )
    return profiles


def load_api_key(path: Path) -> str:
    """Read only DECODO_API_KEY from a private env-style mounted secret."""
    stat = path.stat()
    if stat.st_mode & 0o077:
        raise ProxyDenied(f"proxy API secret {path} must not be accessible by group or other users")
    found: str | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if separator and name.strip() == "DECODO_API_KEY":
            found = value.strip().strip('"').strip("'")
    if not found:
        raise ProxyDenied(f"proxy API secret {path} has no DECODO_API_KEY")
    return found


@dataclass
class ProxyLease:
    reservation_id: UUID
    job_id: UUID
    profile: ProxyProfile
    country: str | None
    session: str
    session_minutes: int
    max_bytes: int
    used_bytes: int = 0
    requests: int = 0

    @classmethod
    def build(
        cls, reservation_id: UUID, job_id: UUID, profile: ProxyProfile,
        country: str | None, session_minutes: int, max_bytes: int,
    ) -> ProxyLease:
        return cls(
            reservation_id, job_id, profile, country,
            secrets.token_hex(12), session_minutes, max_bytes,
        )

    @property
    def username(self) -> str:
        return self.profile.username_for(self.country, self.session, self.session_minutes)

    @property
    def url(self) -> str:
        return (
            f"http://{quote(self.username, safe='')}:{quote(self.profile.password, safe='')}"
            f"@{self.profile.host}:{self.profile.port}"
        )

    @property
    def browser_proxy(self) -> dict[str, str]:
        return {
            "server": f"http://{self.profile.host}:{self.profile.port}",
            "username": self.username,
            "password": self.profile.password,
        }

    def ensure_request_allowed(self) -> None:
        if self.used_bytes >= self.max_bytes:
            raise ProxyDenied("job proxy reservation is exhausted")

    def account(self, tx_bytes: int, rx_bytes: int, requests: int = 1) -> None:
        self.used_bytes += max(0, tx_bytes) + max(0, rx_bytes)
        self.requests += max(0, requests)

    @property
    def display_name(self) -> str:
        return f"decodo/{self.profile.name}/{self.country or 'any'}/{self.session[:6]}"


async def reserve(
    connection: AsyncConnection[Any], *, job_id: UUID, profile: str,
    cycle_start: datetime, cycle_end: datetime, requested_bytes: int = DEFAULT_JOB_BYTES,
    pilot: bool = False,
) -> UUID:
    """Atomically reserve across job, day, pilot, and billing-cycle limits."""
    if cycle_start.tzinfo is None or cycle_end.tzinfo is None:
        raise ProxyDenied("proxy billing-cycle boundaries must include UTC offsets")
    now = datetime.now(UTC)
    if not (cycle_start <= now < cycle_end):
        raise ProxyDenied("configured proxy billing cycle is not active")
    async with connection.transaction():
        cycle = await connection.execute(
            """
            select * from catalogue.proxy_budget_cycles
             where provider = 'decodo' and cycle_start = %(start)s
             for update
            """,
            {"start": cycle_start},
        )
        row = await cycle.fetchone()
        if row is None:
            raise ProxyDenied("Decodo billing cycle has not been reconciled and opened")
        if row["cycle_end"] != cycle_end:
            raise ProxyDenied("configured billing-cycle boundary disagrees with the ledger")
        if row["kill_switch"] or not row["reconciliation_ok"] or row["reconciled_at"] is None:
            raise ProxyDenied("Decodo reconciliation is unsafe or the kill switch is active")
        usage_cursor = await connection.execute(
            """
            select
              coalesce(sum(reserved_bytes) filter (where state = 'active'), 0) active,
              coalesce(sum(estimated_bytes) filter (where created_at >= date_trunc('day', now())), 0) daily,
              coalesce(sum(estimated_bytes) filter (where pilot), 0) pilot_used
            from catalogue.proxy_reservations
            where provider = 'decodo' and cycle_start = %(start)s
            """,
            {"start": cycle_start},
        )
        usage = await usage_cursor.fetchone()
        assert usage is not None
        accounted = max(row["provider_reported_bytes"], row["application_bytes"])
        active = usage["active"]
        if accounted + active + requested_bytes > row["operational_bytes"]:
            raise ProxyDenied("Decodo operational billing-cycle ceiling would be exceeded")
        remaining_days = max(1, math.ceil((cycle_end - now).total_seconds() / 86_400))
        dynamic_daily = min(
            row["daily_bytes"], max(0, (row["operational_bytes"] - accounted - active) // remaining_days)
        )
        if usage["daily"] + active + requested_bytes > dynamic_daily:
            raise ProxyDenied("Decodo daily allocation would be exceeded")
        if pilot and (not row["pilot_active"] or usage["pilot_used"] + active + requested_bytes > row["pilot_bytes"]):
            raise ProxyDenied("Decodo pilot allocation would be exceeded")
        cursor = await connection.execute(
            """
            insert into catalogue.proxy_reservations
              (job_id, provider, profile, cycle_start, reserved_bytes, pilot)
            values (%(job)s, 'decodo', %(profile)s, %(start)s, %(bytes)s, %(pilot)s)
            returning id
            """,
            {"job": job_id, "profile": profile, "start": cycle_start,
             "bytes": requested_bytes, "pilot": pilot},
        )
        inserted = await cursor.fetchone()
        assert inserted is not None
        return inserted["id"]


async def close_reservation(connection: AsyncConnection[Any], lease: ProxyLease) -> None:
    """Close a reservation and monotonically advance application accounting."""
    async with connection.transaction():
        cursor = await connection.execute(
            """
            update catalogue.proxy_reservations
               set estimated_bytes = greatest(estimated_bytes, %(bytes)s),
                   request_count = greatest(request_count, %(requests)s),
                   state = 'closed', closed_at = now()
             where id = %(id)s and state = 'active'
            returning provider, cycle_start, estimated_bytes
            """,
            {"id": lease.reservation_id, "bytes": lease.used_bytes, "requests": lease.requests},
        )
        row = await cursor.fetchone()
        if row:
            await connection.execute(
                """
                update catalogue.proxy_budget_cycles
                   set application_bytes = greatest(
                     application_bytes,
                     (select coalesce(sum(estimated_bytes), 0)
                        from catalogue.proxy_reservations
                       where provider = %(provider)s and cycle_start = %(start)s
                         and state = 'closed')
                   ),
                   kill_switch = kill_switch or (
                     select coalesce(sum(estimated_bytes), 0) >= operational_bytes
                       from catalogue.proxy_reservations
                      where provider = %(provider)s and cycle_start = %(start)s
                        and state = 'closed'
                   )
                 where provider = %(provider)s and cycle_start = %(start)s
                """,
                {"provider": row["provider"], "start": row["cycle_start"]},
            )


async def reconcile(
    connection: AsyncConnection[Any], *, cycle_start: datetime,
    provider_reported_bytes: int, successful: bool,
) -> None:
    """Record provider usage without ever automatically lowering the ledger."""
    await connection.execute(
        """
        update catalogue.proxy_budget_cycles
           set provider_reported_bytes = greatest(provider_reported_bytes, %(reported)s),
               reconciled_at = case when %(ok)s then now() else reconciled_at end,
               reconciliation_ok = %(ok)s,
               kill_switch = kill_switch or greatest(provider_reported_bytes, %(reported)s) >= operational_bytes
         where provider = 'decodo' and cycle_start = %(start)s
        """,
        {"reported": max(0, provider_reported_bytes), "ok": successful, "start": cycle_start},
    )


async def open_cycle(
    connection: AsyncConnection[Any], *, cycle_start: datetime, cycle_end: datetime,
    provider_reported_bytes: int,
) -> None:
    """Open the exact dashboard-confirmed cycle; never infer a calendar reset."""
    if cycle_start.tzinfo is None or cycle_end.tzinfo is None or cycle_end <= cycle_start:
        raise ProxyDenied("billing-cycle boundaries must be ordered offset-aware timestamps")
    overlap = await connection.execute(
        """select 1 from catalogue.proxy_budget_cycles
            where provider = 'decodo' and cycle_start <> %(start)s
              and tstzrange(cycle_start, cycle_end, '[)') && tstzrange(%(start)s, %(end)s, '[)')""",
        {"start": cycle_start, "end": cycle_end},
    )
    if await overlap.fetchone():
        raise ProxyDenied("Decodo billing cycle overlaps an existing ledger cycle")
    await connection.execute(
        """
        insert into catalogue.proxy_budget_cycles
          (provider, cycle_start, cycle_end, provider_reported_bytes,
           reconciled_at, reconciliation_ok, kill_switch)
        values ('decodo', %(start)s, %(end)s, %(reported)s, now(), true,
                %(reported)s >= 2400000000)
        on conflict (provider, cycle_start) do update
          set provider_reported_bytes = greatest(
                catalogue.proxy_budget_cycles.provider_reported_bytes,
                excluded.provider_reported_bytes),
              reconciled_at = now(), reconciliation_ok = true,
              kill_switch = catalogue.proxy_budget_cycles.kill_switch
                         or excluded.provider_reported_bytes >= catalogue.proxy_budget_cycles.operational_bytes
        """,
        {"start": cycle_start, "end": cycle_end, "reported": max(0, provider_reported_bytes)},
    )


def secret_values(profiles: dict[str, ProxyProfile]) -> set[str]:
    return {
        value
        for profile in profiles.values()
        for value in (profile.username, profile.password, profile.api_key)
        if value
    }


def scrub_secrets(value: str, secrets_to_remove: set[str]) -> str:
    cleaned = redact_url(value)
    for secret in secrets_to_remove:
        if secret:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    return cleaned


async def provider_usage(
    profile: ProxyProfile,
    cycle_start: datetime,
    cycle_end: datetime,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Read Decodo's billing-cycle upload+download total in decimal bytes."""
    if not profile.api_key:
        raise ProxyDenied(f"proxy profile {profile.name!r} has no Decodo statistics API key")
    fmt = "%Y-%m-%d %H:%M:%S"
    async with httpx.AsyncClient(transport=transport, timeout=30) as client:
        response = await client.post(
            "https://api.decodo.com/api/v2/statistics/traffic",
            headers={"authorization": profile.api_key, "accept": "application/json"},
            json={
                "proxyType": "residential_proxies",
                "startDate": cycle_start.astimezone(UTC).strftime(fmt),
                "endDate": min(cycle_end, datetime.now(UTC)).astimezone(UTC).strftime(fmt),
                "groupBy": "day",
                "limit": 500,
                "page": 1,
                "sortBy": "grouping_key",
                "sortOrder": "asc",
            },
        )
        response.raise_for_status()
        payload = response.json()
    try:
        return max(0, int(payload["metadata"]["totals"]["total_rx_tx"]))
    except (KeyError, TypeError, ValueError) as error:
        raise ProxyDenied("Decodo statistics response omitted total_rx_tx") from error
