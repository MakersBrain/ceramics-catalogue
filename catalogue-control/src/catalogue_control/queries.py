"""Every read and write the operator API makes.

Kept as SQL in one module rather than spread through the routes, because the
routes are transport adapters and the interesting decisions here are all
relational: what counts as the last run of a source, what "stale" means, what a
cancel is allowed to touch.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

Connection = psycopg.AsyncConnection[dict[str, Any]]


async def one(connection: Connection, sql: str, params: Any = None) -> dict[str, Any] | None:
    async with connection.cursor() as cursor:
        await cursor.execute(sql, params)
        return await cursor.fetchone()


async def all_rows(connection: Connection, sql: str, params: Any = None) -> list[dict[str, Any]]:
    async with connection.cursor() as cursor:
        await cursor.execute(sql, params)
        return await cursor.fetchall()


async def execute(connection: Connection, sql: str, params: Any = None) -> int:
    async with connection.cursor() as cursor:
        await cursor.execute(sql, params)
        return cursor.rowcount


# -- runs -------------------------------------------------------------------

RUNS = """
select r.id, r.kind, r.status, r.requested_by, r.created_at, r.started_at, r.finished_at,
       r.params, r.summary, r.schedule_id,
       count(j.id) as jobs,
       count(j.id) filter (where j.state = 'succeeded') as succeeded,
       count(j.id) filter (where j.state = 'failed')    as failed,
       count(j.id) filter (where j.state in ('queued','leased','running','paused')) as active
  from catalogue.runs r
  left join catalogue.jobs j on j.run_id = r.id
 where (%(cursor)s::timestamptz is null or r.created_at < %(cursor)s::timestamptz)
 group by r.id
 order by r.created_at desc
 limit %(limit)s
"""

RUN = "select * from catalogue.runs where id = %(id)s"

RUN_JOBS = """
select j.id, j.source_id, j.host, j.state, j.attempt, j.max_attempts, j.priority,
       j.scheduled_for, j.started_at, j.finished_at, j.error, j.requires,
       j.lease_owner, j.lease_expires_at, j.cancel_requested, j.pause_requested,
       j.trace_id, j.artifact_path, j.artifact_sha256, j.artifact_size, j.summary,
       p.phase, p.records, p.requests, p.rendered_pages, p.error_count,
       p.discovered, p.truncated, p.in_flight, p.updated_at as progress_at,
       -- The previous run's record count, so the UI can draw a bar against
       -- something rather than an unbounded number.
       (select (previous.summary->>'records')::int
          from catalogue.jobs previous
         where previous.source_id = j.source_id
           and previous.state = 'succeeded'
           and previous.finished_at < coalesce(j.finished_at, now())
         order by previous.finished_at desc limit 1) as previous_records
  from catalogue.jobs j
  left join catalogue.job_progress p on p.job_id = j.id
 where j.run_id = %(run)s
 order by j.source_id
"""

CANCEL_RUN = """
update catalogue.jobs
   set cancel_requested = true
 where run_id = %(run)s
   and state in ('queued', 'leased', 'running', 'paused')
returning id, source_id
"""


# -- jobs -------------------------------------------------------------------

JOB = """
select j.*, r.kind as run_kind,
       p.phase, p.records, p.requests, p.rendered_pages, p.error_count,
       p.discovered, p.truncated, p.in_flight, p.updated_at as progress_at
  from catalogue.jobs j
  join catalogue.runs r on r.id = j.run_id
  left join catalogue.job_progress p on p.job_id = j.id
 where j.id = %(id)s
"""

JOB_LOG = """
select id, at, level, event, message, data
  from catalogue.job_events
 where job_id = %(job)s
   and id > %(after)s
   and (%(level)s::text is null or level = %(level)s::text)
   and (%(search)s::text is null or message ilike '%%' || %(search)s::text || '%%')
 order by id
 limit %(limit)s
"""

# Conditional on the state, so pressing a button twice is a no-op rather than
# an error, and so a job that finished a second ago is not "paused".
PAUSE_JOB = """
update catalogue.jobs
   set pause_requested = true
 where id = %(id)s and state in ('leased', 'running')
returning id, run_id, source_id, state
"""

RESUME_JOB = """
update catalogue.jobs
   set pause_requested = false,
       state = case when state = 'paused' then 'queued' else state end,
       -- An operator resume must not spend the failure budget: pausing is a
       -- human decision about timing, not a failed attempt at the source.
       resume_without_attempt = (state = 'paused'),
       scheduled_for = now(),
       lease_owner = case when state = 'paused' then null else lease_owner end
 where id = %(id)s and (state = 'paused' or pause_requested)
returning id, run_id, source_id, state
"""

CANCEL_JOB = """
update catalogue.jobs
   set cancel_requested = true
 where id = %(id)s and state in ('queued', 'leased', 'running', 'paused')
returning id, run_id, source_id, state
"""

RETRY_JOB = """
update catalogue.jobs
   set state = 'queued',
       cancel_requested = false,
       pause_requested = false,
       -- An explicit retry is a new attempt the operator is asking for, so the
       -- budget is reset rather than bypassed. `resume_without_attempt` is for
       -- pauses, which are a different thing.
       attempt = 0,
       error = null,
       lease_owner = null,
       lease_expires_at = null,
       finished_at = null,
       scheduled_for = now()
 where id = %(id)s and state in ('failed', 'cancelled', 'succeeded', 'skipped')
returning id, run_id, source_id
"""


# -- workers ----------------------------------------------------------------

WORKERS = """
select w.*, j.source_id as current_source,
       extract(epoch from (now() - w.last_heartbeat_at)) as heartbeat_age_seconds
  from catalogue.workers w
  left join catalogue.jobs j on j.id = w.current_job_id
 where w.status <> 'stopped' or w.last_heartbeat_at > now() - interval '1 hour'
 order by w.started_at desc
"""

SET_WORKER_STATE = """
update catalogue.workers
   set desired_state = %(desired)s
 where id = %(id)s and status <> 'stopped'
returning id, status, desired_state
"""

#: Hiding is a roster operation, not a signal to a process. Only an instance
#: that has already missed the UI's lost-heartbeat threshold may be hidden;
#: this prevents an operator from making healthy capacity disappear while it
#: continues to claim work.
HIDE_LOST_WORKER = """
update catalogue.workers
   set status = 'stopped', current_job_id = null
 where id = %(id)s
   and status <> 'stopped'
   and last_heartbeat_at < now() - interval '30 seconds'
returning id, status, desired_state
"""

#: A worker is lost when it has missed three heartbeat intervals. Durable,
#: because "the worker died at 03:12" is exactly the thing nobody was watching
#: for.
LOST_WORKERS = """
select id, hostname, last_heartbeat_at
  from catalogue.workers
 where status not in ('stopped')
   and last_heartbeat_at < now() - make_interval(secs => %(seconds)s)
"""


# -- sources ----------------------------------------------------------------

# Driven by the union of "has settings" and "has history", not by either alone.
#
# Selecting from `catalogue.jobs` on its own loses a source that has been
# disabled but never run — which is exactly the source an operator most wants to
# see on this page, because it is the one that is not going to appear in any
# future run and nothing else would say so.
SOURCES = """
with known as (
  select source_id from catalogue.jobs
  union
  select source_id from catalogue.source_settings
),
history as (
  select j.source_id,
         max(j.finished_at) filter (where j.state = 'succeeded') as last_success_at,
         (array_agg((j.summary->>'records')::int order by j.finished_at desc)
           filter (where j.state = 'succeeded'))[1] as last_records,
         (array_agg((j.summary->>'records')::int order by j.finished_at desc)
           filter (where j.state = 'succeeded'))[2] as previous_records,
         count(*) filter (where j.finished_at > now() - interval '7 days') as runs_7d,
         count(*) filter (where j.state = 'failed'
                            and j.finished_at > now() - interval '7 days') as failures_7d
    from catalogue.jobs j
   group by j.source_id
)
select k.source_id,
       coalesce(t.enabled, true)  as enabled,
       coalesce(t.paused, false)  as paused,
       t.schedule_id,
       coalesce(t.params, '{}'::jsonb) as params,
       h.last_success_at,
       h.last_records,
       h.previous_records,
       coalesce(h.runs_7d, 0)     as runs_7d,
       coalesce(h.failures_7d, 0) as failures_7d,
       extract(epoch from (now() - h.last_success_at)) as staleness_seconds
  from known k
  left join history h on h.source_id = k.source_id
  left join catalogue.source_settings t on t.source_id = k.source_id
 order by k.source_id
"""

UPSERT_SOURCE = """
insert into catalogue.source_settings (source_id, enabled, paused, schedule_id, params, updated_by)
values (%(id)s, %(enabled)s, %(paused)s, %(schedule)s, %(params)s, %(by)s)
on conflict (source_id) do update
   set enabled = excluded.enabled,
       paused = excluded.paused,
       schedule_id = excluded.schedule_id,
       params = excluded.params,
       updated_at = now(),
       updated_by = excluded.updated_by
returning *
"""

#: Pausing a source also pauses the jobs it already has in flight. Resuming does
#: *not* automatically resume individually paused jobs — a broad administrative
#: toggle must not silently restart work somebody stopped on purpose.
PAUSE_SOURCE_JOBS = """
update catalogue.jobs
   set pause_requested = true
 where source_id = %(id)s and state in ('leased', 'running')
returning id
"""


# -- notifications ----------------------------------------------------------

NOTIFICATIONS = """
select * from catalogue.notifications
 where (%(unacknowledged)s::boolean is not true
        or (acknowledged_at is null and resolved_at is null))
   and (%(severity)s::text is null or severity = %(severity)s::text)
 order by acknowledged_at is not null, at desc
 limit %(limit)s
"""


# -- schedules --------------------------------------------------------------

SCHEDULES = "select * from catalogue.schedules order by id"

UPSERT_SCHEDULE = """
insert into catalogue.schedules (id, enabled, cron, timezone, source_filter, params)
values (%(id)s, %(enabled)s, %(cron)s, %(timezone)s, %(filter)s, %(params)s)
on conflict (id) do update
   set enabled = excluded.enabled,
       cron = excluded.cron,
       timezone = excluded.timezone,
       source_filter = excluded.source_filter,
       params = excluded.params
returning *
"""


# -- bootstrap --------------------------------------------------------------


async def bootstrap(connection: Connection, run_id: UUID | None = None) -> dict[str, Any]:
    """The first frame of a stream: everything needed to render without a fetch.

    A client that had to make a second request to draw anything would show an
    empty page for a round trip, and would have to reconcile whatever arrived on
    the stream in between with whatever the fetch returned.
    """
    from catalogue_control.broker import WORKER_ROSTER, _worker

    workers = await all_rows(connection, WORKER_ROSTER)
    active = await all_rows(
        connection,
        "select id, kind, status, started_at, created_at from catalogue.runs "
        "where status in ('queued','running') order by created_at desc limit 5",
    )
    unacknowledged = await all_rows(
        connection, NOTIFICATIONS, {"unacknowledged": True, "severity": None, "limit": 50}
    )
    payload: dict[str, Any] = {
        "workers": [_worker(row) for row in workers],
        "active_runs": active,
        "notifications": unacknowledged,
        "queue": await queue_depth(connection),
    }
    if run_id is not None:
        # Progress subscribers get the current snapshot for every live job, so
        # the bars are populated before the first tick rather than after it.
        payload["jobs"] = await all_rows(connection, RUN_JOBS, {"run": run_id})
    return payload


async def queue_depth(connection: Connection) -> dict[str, int]:
    rows = await all_rows(
        connection,
        "select state, count(*) as n from catalogue.jobs "
        "where state not in ('succeeded','cancelled','skipped') group by state",
    )
    return {row["state"]: int(row["n"]) for row in rows}


def as_jsonb(value: Any) -> Jsonb:
    return Jsonb(value if value is not None else {})
