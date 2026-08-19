"""Bringing a source back when the site it reads has recovered.

A supplier's site goes down and there is nothing to be done about it from here.
spectrum's WordPress lost its database on 2026-08-16; every route needing a live
query — the WooCommerce Store API, the sitemap, any page not already on disk —
answered 500 for days, while WP Rocket kept serving a frozen 2026-08-14 copy of
about forty per cent of the storefront. Retrying that is pointless and scraping
it is worse than pointless: it would load five-day-old prices as today's.

So the source comes out of runs. The cost of taking it out is that nothing puts
it back, and a source quietly missing from a price catalogue is a worse failure
than a source loudly failing in it — nobody remembers, on the Tuesday three
weeks later when the supplier fixes their database, that they were meant to.
This module is the half that remembers.

Disabling rather than pausing is deliberate. `source_settings.paused` still has
a job created for the source, and `queue.reserve` acknowledges that job without
moving it out of `queued`, so the run it belongs to never closes. `enabled =
false` creates no job at all and runs close normally.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import psycopg

from mb_ceramics_catalogue.observability import logging as obs
from mb_ceramics_catalogue.ops import events

LOGGER = obs.get_logger("catalogue.health")

Connection = psycopg.AsyncConnection[dict[str, Any]]

#: How often the leader probes the sites it is waiting on. Half an hour: a
#: supplier restoring a database is not a thing that needs noticing in seconds,
#: and a disabled source costs nothing while we wait.
PROBE_SECONDS = 1800.0

#: Long enough for a site coming back under load, short enough that a leader
#: tick is never held up by one unreachable host.
TIMEOUT_SECONDS = 20.0

#: Sent instead of the crawler's own identity: this is a health check, and it
#: should get whatever an ordinary visitor gets.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

PENDING = """
select source_id, url, expect, reason, consecutive_ok, required_ok
  from catalogue.source_health_probes
 where recovered_at is null
 order by last_checked_at nulls first
"""


async def disable(
    connection: Connection,
    source_id: str,
    *,
    url: str,
    reason: str,
    expect: str = "json",
    required_ok: int = 2,
) -> None:
    """Take a source out of future runs, and record how to tell when it is back.

    Both halves in one transaction on purpose: a source disabled without a probe
    is one nobody will re-enable, which is the outcome this exists to avoid.
    """
    async with connection.transaction():
        await connection.execute(
            """
            insert into catalogue.source_settings (source_id, enabled, updated_by)
                 values (%(source)s, false, 'health-probe')
            on conflict (source_id) do update
                    set enabled = false, updated_by = 'health-probe', updated_at = now()
            """,
            {"source": source_id},
        )
        await connection.execute(
            """
            insert into catalogue.source_health_probes
                        (source_id, url, expect, reason, required_ok)
                 values (%(source)s, %(url)s, %(expect)s, %(reason)s, %(required)s)
            on conflict (source_id) do update
                    set url = excluded.url, expect = excluded.expect,
                        reason = excluded.reason, required_ok = excluded.required_ok,
                        disabled_at = now(), recovered_at = null,
                        consecutive_ok = 0, checks = 0
            """,
            {"source": source_id, "url": url, "expect": expect,
             "reason": reason, "required": required_ok},
        )
        # The warnings this source raised on its way down describe a condition
        # that has been superseded, and neither can clear itself any more: both
        # are written to resolve on the source's next success, and a source out
        # of runs has no next success. Left open they sit on the operator's
        # screen for as long as the supplier takes, which is how a screen full
        # of warnings stops being read.
        for stale in (f"source.stale:{source_id}", f"job.failed:{source_id}"):
            await events.resolve(connection, stale, source_id=source_id)
        await events.notify(
            connection,
            "source.disabled",
            f"{source_id} is out of runs until its site recovers",
            body=f"{reason} Probing {url} every {int(PROBE_SECONDS // 60)} minutes.",
            dedup_key=f"source.disabled:{source_id}",
            source_id=source_id,
        )
    LOGGER.warning("source.disabled", source=source_id, url=url, reason=reason)


async def check_recovered(connection: Connection, *, client: Any = None) -> int:
    """Probe every source waiting on a recovery; re-enable the ones that answered.

    Returns how many were re-enabled. Never raises: this runs inside the leader
    tick, where a supplier's unreachable host must not stop scheduling.
    """
    cursor = await connection.execute(PENDING)
    pending = await cursor.fetchall()
    if not pending:
        return 0

    owned = client is None
    if owned:
        client = httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
    recovered = 0
    try:
        for row in pending:
            status, error = await _probe(client, row["url"], row["expect"])
            if await _record(connection, row, status, error):
                recovered += 1
    finally:
        if owned:
            await client.aclose()
    return recovered


async def _probe(client: Any, url: str, expect: str) -> tuple[int | None, str | None]:
    """What the site said, as (status, why it does not count as recovered)."""
    try:
        response = await client.get(url)
    except Exception as error:  # noqa: BLE001 - any transport fault is "not back yet"
        return None, f"{type(error).__name__}: {error}"[:500]
    if response.status_code != 200:
        return response.status_code, f"HTTP {response.status_code}"
    if expect == "json":
        # A 200 is not enough. The failure being waited on here is a site whose
        # cache answers while its database does not, and the difference shows up
        # as a body that will not parse.
        try:
            json.loads(response.text)
        except ValueError:
            return response.status_code, "200 but the body is not JSON"
    return response.status_code, None


async def _record(
    connection: Connection, row: dict[str, Any], status: int | None, error: str | None
) -> bool:
    """Fold one probe result in, and re-enable the source if it has held up."""
    source = row["source_id"]
    if error is not None:
        await connection.execute(
            """
            update catalogue.source_health_probes
               set last_checked_at = now(), last_status = %(status)s, last_error = %(error)s,
                   checks = checks + 1, consecutive_ok = 0
             where source_id = %(source)s
            """,
            {"source": source, "status": status, "error": error},
        )
        LOGGER.info("source.probe_failed", source=source, status=status, error=error)
        return False

    updated = await connection.execute(
        """
        update catalogue.source_health_probes
           set last_checked_at = now(), last_status = %(status)s, last_error = null,
               checks = checks + 1, consecutive_ok = consecutive_ok + 1
         where source_id = %(source)s
        returning consecutive_ok, required_ok
        """,
        {"source": source, "status": status},
    )
    result = await updated.fetchone()
    assert result is not None
    if result["consecutive_ok"] < result["required_ok"]:
        # Answering once proves the request went through, not that the site is
        # serving its own data again. Wait for it to hold.
        LOGGER.info(
            "source.probe_ok", source=source,
            consecutive_ok=result["consecutive_ok"], required_ok=result["required_ok"],
        )
        return False

    async with connection.transaction():
        await connection.execute(
            """
            insert into catalogue.source_settings (source_id, enabled, updated_by)
                 values (%(source)s, true, 'health-probe')
            on conflict (source_id) do update
                    set enabled = true, updated_by = 'health-probe', updated_at = now()
            """,
            {"source": source},
        )
        await connection.execute(
            "update catalogue.source_health_probes set recovered_at = now() "
            "where source_id = %(source)s",
            {"source": source},
        )
        # The condition that took the source out has ended, so the warning that
        # announced it goes with it rather than sitting on the operator's screen.
        await events.resolve(connection, f"source.disabled:{source}", source_id=source)
        await events.notify(
            connection,
            "source.recovered",
            f"{source} is back in runs",
            severity=events.Severity.INFO,
            body=f"{row['url']} answered {result['required_ok']} times running.",
            dedup_key=f"source.recovered:{source}",
            source_id=source,
        )
    LOGGER.warning("source.recovered", source=source, url=row["url"])
    return True
