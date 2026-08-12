"""The conditions worth waking somebody for, and the pruning nobody should think about.

Two of these justify the whole notification path. A scraper that quietly starts
returning half a catalogue, and a shop that has started refusing us, are both
invisible in a run that reports success — and both are the reason a daily price
feed can be wrong for a fortnight before anyone notices.

Every rule is written to **clear itself**. `source.stale` resolves when the
source succeeds; `host.blocking` resolves when the host stops refusing. An alert
that never clears is one people learn to ignore, which is worse than no alert.

Thresholds are deliberately conservative and warning-only to begin with. §13 of
the plan is explicit that `source.shrank` at a fixed percentage will be noisy for
small sources and blind for large ones; the numbers here are a starting point to
be tuned against a fortnight of real runs, not a finished answer.
"""

from __future__ import annotations

from typing import Any

import psycopg

from mb_ceramics_catalogue.observability import logging as obs
from mb_ceramics_catalogue.ops import events

LOGGER = obs.get_logger("catalogue.monitor")

Connection = psycopg.AsyncConnection[dict[str, Any]]

#: Consecutive runs with no records before a source is called stale. Three,
#: because one empty run is a bad afternoon and three is a broken scraper.
STALE_RUNS = 3

#: Proportional drop that counts as a source having shrunk. A large storefront
#: legitimately moves a few per cent between days.
SHRANK_SHARE = 0.30

#: Below this, proportional change is noise: a 12-product source losing four is
#: a 33% drop and means nothing.
SHRANK_FLOOR = 50

#: Missed heartbeat intervals before a worker is declared lost.
LOST_INTERVALS = 3
HEARTBEAT_SECONDS = 5

#: Share of a host's recent requests that must be refusals before it counts as
#: blocking us.
BLOCKING_SHARE = 0.25
BLOCKING_MINIMUM = 20

#: Retention. `runs` and `jobs` are kept indefinitely — they are small and they
#: are the history. Logs and the event stream are not.
EVENT_RETENTION_DAYS = 30
NOTIFICATION_RETENTION_DAYS = 90


async def check_all(connection: Connection) -> dict[str, int]:
    """Run every rule. Called by the leader, on its own cadence."""
    raised = {
        "source.stale": await check_stale_sources(connection),
        "source.shrank": await check_shrunk_sources(connection),
        "worker.lost": await check_lost_workers(connection),
        "host.blocking": await check_blocking_hosts(connection),
    }
    LOGGER.debug("monitor.checked", **raised)
    return raised


STALE = """
with recent as (
  select source_id,
         (summary->>'records')::int as records,
         row_number() over (partition by source_id order by finished_at desc) as rank
    from catalogue.jobs
   where state in ('succeeded', 'failed') and finished_at is not null
)
select source_id,
       count(*) filter (where coalesce(records, 0) = 0) as empty_runs,
       count(*) as considered
  from recent
 where rank <= %(runs)s
 group by source_id
having count(*) >= %(runs)s
"""


async def check_stale_sources(connection: Connection) -> int:
    """A source that has returned nothing for N consecutive runs.

    The staleness badge on `/ops/sources` is the single most useful thing on that
    page, and this is the durable half of it: a source that silently stopped
    returning records is the failure this whole plan exists to catch.
    """
    raised = 0
    for row in await _all(connection, STALE, {"runs": STALE_RUNS}):
        source = row["source_id"]
        key = f"source.stale:{source}"
        if row["empty_runs"] >= STALE_RUNS:
            if await events.notify(
                connection,
                "source.stale",
                f"{source} has returned no records for {STALE_RUNS} runs",
                body="The scraper is running and finding nothing, which usually means the shop changed.",
                dedup_key=key,
                source_id=source,
            ):
                raised += 1
        else:
            # It has produced records again. The condition has ended, so the
            # warning should stop being shown.
            await events.resolve(connection, key, source_id=source)
    return raised


SHRANK = """
with recent as (
  select source_id,
         (summary->>'records')::int as records,
         finished_at,
         row_number() over (partition by source_id order by finished_at desc) as rank
    from catalogue.jobs
   where state = 'succeeded' and finished_at is not null
)
select current.source_id,
       current.records  as now_records,
       previous.records as then_records
  from recent current
  join recent previous
    on previous.source_id = current.source_id and previous.rank = 2
 where current.rank = 1
   and previous.records >= %(floor)s
   and current.records < previous.records * (1 - %(share)s)
"""


async def check_shrunk_sources(connection: Connection) -> int:
    """A source whose record count fell sharply against its previous run.

    This and `host.blocking` are the two rules that justify the notification
    path at all: a run where one source returned half a catalogue reports
    success, and the halving is only visible if something is looking for it.
    """
    raised = 0
    for row in await _all(connection, SHRANK, {"floor": SHRANK_FLOOR, "share": SHRANK_SHARE}):
        source, now, before = row["source_id"], row["now_records"], row["then_records"]
        share = 100 * (before - now) / before
        if await events.notify(
            connection,
            "source.shrank",
            f"{source} returned {now} records, down {share:.0f}% from {before}",
            body="Either the shop withdrew stock or the scraper stopped seeing part of it.",
            # Keyed on the pair, so one genuine reduction is reported once and a
            # further drop next week is reported again.
            dedup_key=f"source.shrank:{source}:{before}:{now}",
            source_id=source,
        ):
            raised += 1
    return raised


async def check_lost_workers(connection: Connection) -> int:
    """A worker that stopped reporting without saying it was going.

    Critical, and durable, because this is the case where events cannot help:
    nothing fires when a process stops existing, and the browser-side staleness
    badge only helps whoever happened to be looking.
    """
    raised = 0
    rows = await _all(
        connection,
        "select id, hostname, last_heartbeat_at from catalogue.workers "
        "where status not in ('stopped') and last_heartbeat_at < now() - make_interval(secs => %(s)s)",
        {"s": HEARTBEAT_SECONDS * LOST_INTERVALS},
    )
    for row in rows:
        if await events.notify(
            connection,
            "worker.lost",
            f"worker on {row['hostname']} stopped reporting",
            body=f"Last heartbeat {row['last_heartbeat_at']}. Its jobs will be recovered when their leases expire.",
            severity=events.Severity.CRITICAL,
            dedup_key=f"worker.lost:{row['id']}",
            worker_id=row["id"],
        ):
            raised += 1
    return raised


BLOCKING = """
select j.host,
       count(*) filter (where e.level = 'error') as errors,
       count(*) as considered
  from catalogue.job_events e
  join catalogue.jobs j on j.id = e.job_id
 where e.at > now() - interval '1 hour'
   and (e.message ilike '%%403%%' or e.message ilike '%%429%%' or e.level = 'error')
 group by j.host
having count(*) >= %(minimum)s
"""


async def check_blocking_hosts(connection: Connection) -> int:
    """A host whose refusal rate has crossed a threshold.

    Critical, because getting a source blocked costs more than any feature in
    this plan is worth, and because the remedy — lower `catalogue.hosts`
    concurrency, raise the delay — has to be applied before the shop escalates.
    """
    raised = 0
    for row in await _all(connection, BLOCKING, {"minimum": BLOCKING_MINIMUM}):
        share = row["errors"] / row["considered"] if row["considered"] else 0
        key = f"host.blocking:{row['host']}"
        if share >= BLOCKING_SHARE:
            if await events.notify(
                connection,
                "host.blocking",
                f"{row['host']} refused {row['errors']} of {row['considered']} recent requests",
                body="Lower its max_concurrency in catalogue.hosts, or give it a delay, before it escalates.",
                severity=events.Severity.CRITICAL,
                dedup_key=key,
            ):
                raised += 1
        else:
            await events.resolve(connection, key)
    return raised


async def check_degraded_run(connection: Connection, run_id: Any) -> bool:
    """Raise `run.degraded` for a run that finished with a failed source."""
    row = await _one(
        connection,
        "select status, summary from catalogue.runs where id = %(id)s and status = 'degraded'",
        {"id": run_id},
    )
    if row is None:
        return False
    summary = row["summary"] or {}
    return bool(
        await events.notify(
            connection,
            "run.degraded",
            f"a run completed with {summary.get('failed', 0)} failed source(s)",
            run_id=run_id,
            dedup_key=f"run.degraded:{run_id}",
        )
    )


async def prune(connection: Connection) -> dict[str, int]:
    """Delete what is past its retention window.

    An **unresolved notification is never pruned**, however old. It is the thing
    nobody dealt with, and deleting it would turn "we never fixed that" into "we
    have no record of that".
    """
    removed = {}
    removed["job_events"] = await _execute(
        connection,
        "delete from catalogue.job_events where at < now() - make_interval(days => %(days)s)",
        {"days": EVENT_RETENTION_DAYS},
    )
    removed["event_log"] = await _execute(
        connection,
        "delete from catalogue.event_log where at < now() - make_interval(days => %(days)s)",
        {"days": EVENT_RETENTION_DAYS},
    )
    removed["notifications"] = await _execute(
        connection,
        "delete from catalogue.notifications "
        " where at < now() - make_interval(days => %(days)s)"
        "   and (resolved_at is not null or acknowledged_at is not null)",
        {"days": NOTIFICATION_RETENTION_DAYS},
    )
    if any(removed.values()):
        LOGGER.info("monitor.pruned", **removed)
    return removed


async def _all(connection: Connection, sql: str, params: Any = None) -> list[dict[str, Any]]:
    async with connection.cursor() as cursor:
        await cursor.execute(sql, params)
        return await cursor.fetchall()


async def _one(connection: Connection, sql: str, params: Any = None) -> dict[str, Any] | None:
    async with connection.cursor() as cursor:
        await cursor.execute(sql, params)
        return await cursor.fetchone()


async def _execute(connection: Connection, sql: str, params: Any = None) -> int:
    async with connection.cursor() as cursor:
        await cursor.execute(sql, params)
        return max(cursor.rowcount, 0)
