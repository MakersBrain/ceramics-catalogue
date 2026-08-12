"""Firing schedules, on whichever worker holds the advisory lock.

No separate scheduler container. Every worker tries `pg_try_advisory_xact_lock`
on a fixed key each tick; whichever holds it materialises due schedules into runs
and jobs. One less thing to deploy, and no single point of failure.

The lock is **transaction-scoped** on purpose. A session-scoped one can leak, and
on a pooled connection the lock counts accumulate against whichever session
happens to be handed out — so the leader would eventually be "whoever holds a
connection nobody released".

Firing is **idempotent, not merely mutually exclusive.** Mutual exclusion stops
two leaders firing at once; it does nothing about one leader that commits a run
and then dies before advancing the cursor. The unique index on
`(schedule_id, scheduled_fire_at)` is what makes the next tick a no-op for that
occurrence instead of a second run of eighty sources.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from croniter import croniter

from mb_ceramics_catalogue.config.sources import SourcesFile
from mb_ceramics_catalogue.observability import logging as obs
from mb_ceramics_catalogue.ops import events, runs

LOGGER = obs.get_logger("catalogue.schedule")

Connection = psycopg.AsyncConnection[dict[str, Any]]

#: The advisory lock key. Arbitrary but fixed; anything else in this database
#: taking advisory locks must not pick the same one.
LEADER_KEY = 0x0CA7_A106  # "catalogue"


async def try_become_leader(connection: Connection) -> bool:
    """Take the transaction-scoped leader lock, or report that someone else has.

    Must be called inside a transaction; the lock is released when it ends,
    which is precisely the property that stops it leaking on a pooled session.
    """
    row = await _one(
        connection, "select pg_try_advisory_xact_lock(%(key)s) as held", {"key": LEADER_KEY}
    )
    return bool(row and row["held"])


def next_fire(cron: str, timezone: str, after: datetime | None = None) -> datetime:
    """The next occurrence of a cron expression, in its own timezone.

    Computed in the schedule's zone and returned as UTC. Doing the arithmetic in
    UTC instead would silently move a 03:00 Europe/Paris run to 02:00 or 04:00
    for half the year, which is exactly the kind of thing nobody notices until a
    daily job runs twice on one October morning.
    """
    zone = ZoneInfo(timezone)
    reference = (after or datetime.now(UTC)).astimezone(zone)
    return croniter(cron, reference).get_next(datetime).astimezone(UTC)  # type: ignore[no-any-return]


def previous_fire(cron: str, timezone: str, before: datetime | None = None) -> datetime:
    zone = ZoneInfo(timezone)
    reference = (before or datetime.now(UTC)).astimezone(zone)
    return croniter(cron, reference).get_prev(datetime).astimezone(UTC)  # type: ignore[no-any-return]


DUE = """
select id, cron, timezone, source_filter, params, last_fired_at, next_fire_at
  from catalogue.schedules
 where enabled
   and (next_fire_at is null or next_fire_at <= now())
 order by id
 for update skip locked
"""


async def fire_due(connection: Connection, sources: SourcesFile) -> list[dict[str, Any]]:
    """Materialise every due schedule into a run. Called by the leader only.

    Each schedule is handled in the same transaction that locked its row, so a
    rollback advances neither the run nor the cursor, and a crash after commit
    leaves the occurrence key to make the retry a no-op.
    """
    fired = []
    for schedule in await _all(connection, DUE):
        with obs.bound(schedule_id=schedule["id"]):
            result = await _fire(connection, schedule, sources)
            if result:
                fired.append(result)
    return fired


async def _fire(
    connection: Connection, schedule: dict[str, Any], sources: SourcesFile
) -> dict[str, Any] | None:
    now = datetime.now(UTC)
    cron, timezone = schedule["cron"], schedule["timezone"]

    # A schedule that has never fired starts from now rather than from the epoch;
    # otherwise enabling one would immediately materialise its most recent past
    # occurrence, which is a surprising thing for "save" to do.
    fire_at = schedule["next_fire_at"] or previous_fire(cron, timezone, now)

    missed = _missed_between(cron, timezone, fire_at, now)
    if len(missed) > 1:
        # Materialise at most the most recent missed occurrence after downtime.
        # A catch-up herd of eighty jobs per missed night would hammer every
        # shop in the catalogue at once, which is the opposite of what a
        # politeness-obsessed crawler should do after an outage.
        skipped = missed[:-1]
        fire_at = missed[-1]
        await events.notify(
            connection,
            "schedule.missed",
            f"{schedule['id']} missed {len(skipped)} occurrence(s)",
            body=(
                "The most recent missed occurrence was materialised; the earlier "
                f"{len(skipped)} were not, to avoid a catch-up herd."
            ),
            severity=events.Severity.CRITICAL,
            dedup_key=f"schedule.missed:{schedule['id']}:{fire_at.isoformat()}",
        )

    run_id = await runs.create_run(
        connection,
        kind="scheduled",
        schedule_id=schedule["id"],
        scheduled_fire_at=fire_at,
        requested_by=f"schedule:{schedule['id']}",
        params=dict(schedule["params"] or {}),
    )

    upcoming = next_fire(cron, timezone, now)
    await _execute(
        connection,
        "update catalogue.schedules set last_fired_at = %(fired)s, next_fire_at = %(next)s "
        "where id = %(id)s",
        {"id": schedule["id"], "fired": fire_at if run_id else schedule["last_fired_at"],
         "next": upcoming},
    )

    if run_id is None:
        # The occurrence key rejected it: some earlier leader already created
        # this run and died before advancing the cursor. Advancing it here is
        # the whole recovery.
        LOGGER.info("schedule.already_fired", fire_at=fire_at.isoformat())
        return None

    selected = _select_sources(sources, schedule["source_filter"])
    jobs = await runs.create_jobs(connection, run_id, sources, selected)
    await events.emit(
        connection,
        events.Topic.SCHEDULE,
        "schedule.fired",
        run_id=run_id,
        payload={"schedule_id": schedule["id"], "fire_at": fire_at.isoformat(), "jobs": len(jobs)},
    )
    LOGGER.info(
        "schedule.fired",
        run_id=str(run_id),
        jobs=len(jobs),
        fire_at=fire_at.isoformat(),
        next_fire_at=upcoming.isoformat(),
    )
    return {"schedule_id": schedule["id"], "run_id": run_id, "jobs": len(jobs)}


def _missed_between(cron: str, timezone: str, start: datetime, end: datetime) -> list[datetime]:
    """Every occurrence in (start, end], capped so a long outage cannot explode."""
    zone = ZoneInfo(timezone)
    iterator = croniter(cron, start.astimezone(zone))
    found: list[datetime] = []
    for _ in range(500):
        moment = iterator.get_next(datetime).astimezone(UTC)
        if moment > end:
            break
        found.append(moment)
    return found or [start]


def _select_sources(sources: SourcesFile, source_filter: dict[str, Any] | None) -> list[str]:
    """Which sources a schedule covers.

    `{"all": true}` is everything configured; `{"only": [...]}` and
    `{"except": [...]}` narrow it. Unknown names are ignored rather than fatal:
    a schedule naming a source that was later removed should keep firing for the
    rest, not stop dead every night.
    """
    names = sources.names()
    source_filter = source_filter or {}

    # `only` is checked before `all`, and that order is the whole of the logic:
    # `{"only": [...]}` carries no `all` key, so testing `all` first with a
    # default of True quietly selects everything and the allowlist is ignored.
    # A schedule that says "just these two" then crawls all eighty.
    if only := source_filter.get("only"):
        selected = [name for name in names if name in set(only)]
    else:
        selected = names

    if excluded := source_filter.get("except"):
        selected = [name for name in selected if name not in set(excluded)]
    return selected


async def _one(connection: Connection, sql: str, params: Any = None) -> dict[str, Any] | None:
    async with connection.cursor() as cursor:
        await cursor.execute(sql, params)
        return await cursor.fetchone()


async def _all(connection: Connection, sql: str, params: Any = None) -> list[dict[str, Any]]:
    async with connection.cursor() as cursor:
        await cursor.execute(sql, params)
        return await cursor.fetchall()


async def _execute(connection: Connection, sql: str, params: Any = None) -> int:
    async with connection.cursor() as cursor:
        await cursor.execute(sql, params)
        return cursor.rowcount
