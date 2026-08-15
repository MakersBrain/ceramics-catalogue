"""Claiming work, and recovering it when a worker dies.

Postgres is the queue. `for update skip locked` gives multi-worker claiming, the
volume is eighty jobs a day, and the catalogue already lives here — so there is
no Redis, no broker, and nothing new to operate or back up.

Two rules run through all of this, and both exist because the alternative is
silently wrong:

**Claiming does not consume an attempt.** An attempt begins only once a host slot
has been acquired and the job has actually started. Host contention and a crash
between claim and start are not scraper attempts, and counting them would burn a
source's retry budget on things the source never did.

**A dead worker's job is recovered by its lease expiring**, not by the worker
telling anyone. Nothing fires when a process stops existing, so the only thing
that can be relied upon is the absence of a renewal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import psycopg

from mb_ceramics_catalogue.connectors import BrowserBackendName
from mb_ceramics_catalogue.observability import logging as obs
from mb_ceramics_catalogue.ops import events

LOGGER = obs.get_logger("catalogue.queue")

Connection = psycopg.AsyncConnection[dict[str, Any]]

#: How long a claim is good for without a renewal. Renewed on every heartbeat
#: while the job runs, so this only matters once the worker has stopped
#: heartbeating — that is, once it is gone.
LEASE_SECONDS = 300

#: How long a job waits when its host is busy. No attempt is consumed, so this
#: is pure politeness rather than a retry.
HOST_BACKOFF_SECONDS = 30


CLAIM = """
with candidate as (
  select j.id
    from catalogue.jobs j
    left join catalogue.source_settings s on s.source_id = j.source_id
   where (j.attempt < j.max_attempts or j.resume_without_attempt)
     -- The worker must advertise every capability the job asks for. `<@` is
     -- containment: a plain worker cannot take a job needing a browser, and a
     -- browser worker can still take plain jobs.
     and j.requires <@ %(capabilities)s::text[]
     -- `requires_any` is one optional OR group. A previously selected browser
     -- backend is stricter still: retries stay on that backend unless an
     -- explicit lineage reset clears the snapshot.
     and (cardinality(j.requires_any) = 0
          or j.requires_any && %(capabilities)s::text[])
     and (j.selected_browser_backend is null
          or ('browser:' || j.selected_browser_backend) = any(%(capabilities)s::text[]))
     and coalesce(s.enabled, true)
     and not coalesce(s.paused, false)
     and not j.cancel_requested
     and (
       (j.state = 'queued' and j.scheduled_for <= now())
       -- A lease that expired belongs to a worker that stopped heartbeating.
       or (j.state in ('leased', 'running') and j.lease_expires_at < now())
     )
   order by j.priority, j.scheduled_for
   for update of j skip locked
   limit 1
)
update catalogue.jobs j
   set state = 'leased',
       lease_owner = %(worker)s,
       lease_expires_at = now() + make_interval(secs => %(lease)s)
  from candidate
 where j.id = candidate.id
returning j.*
"""

# Conditional on this worker still owning an unexpired lease. If the lease
# expired between claiming and acquiring a host slot, another worker may already
# have taken the job, and this must not quietly start a second copy of it.
START = """
update catalogue.jobs
   set state = 'running',
       attempt = attempt + case when resume_without_attempt then 0 else 1 end,
       resume_without_attempt = false,
       started_at = coalesce(started_at, now()),
       trace_id = coalesce(%(trace)s, trace_id),
       selected_browser_backend = coalesce(selected_browser_backend, %(backend)s),
       lease_expires_at = now() + make_interval(secs => %(lease)s)
 where id = %(id)s
   and lease_owner = %(worker)s
   and state = 'leased'
   and lease_expires_at > now()
   and (selected_browser_backend is null or selected_browser_backend = %(backend)s)
   and (
     cardinality(requires_any) = 0
     or %(backend_capability)s = any(requires_any)
   )
returning attempt, max_attempts, selected_browser_backend
"""

RENEW = """
update catalogue.jobs
   set lease_expires_at = now() + make_interval(secs => %(lease)s)
 where lease_owner = %(worker)s
   and state in ('leased', 'running')
returning id, cancel_requested, pause_requested
"""

RELEASE = """
update catalogue.jobs
   set state = 'queued',
       lease_owner = null,
       lease_expires_at = null,
       scheduled_for = now() + make_interval(secs => %(delay)s)
 where id = %(id)s
   and lease_owner = %(worker)s
   and state in ('leased', 'running')
returning id
"""

# A job that turned out to need a capability its worker does not have. It goes
# back on the queue carrying the requirement, so the containment test in CLAIM
# now routes it to a worker that can serve it.
#
# `resume_without_attempt` is what keeps this from spending the source's retry
# budget: the attempt was already consumed by START, and the source did nothing
# wrong. Without it, three plain workers picking a browser job up in turn would
# exhaust a source that was never actually crawled.
#
# `scheduled_for` is left alone rather than delayed: the capable worker may be
# free right now, and there is nothing to back off from.
ESCALATE = """
update catalogue.jobs
   set state = 'queued',
       requires = (
         select array(
           select distinct unnest(requires || array[%(capability)s]::text[]) order by 1
         )
       ),
       resume_without_attempt = true,
       lease_owner = null,
       lease_expires_at = null
 where id = %(id)s
   and lease_owner = %(worker)s
   and state in ('leased', 'running')
   and not (%(capability)s = any(requires))
returning id, requires, attempt
"""

# Expired, out of attempts, and not flagged for an operator resume: nothing will
# ever pick this up again, so it must be made terminal rather than left as a
# `running` row that no process is running.
REAP = """
update catalogue.jobs
   set state = 'failed',
       finished_at = now(),
       error = coalesce(error, 'lease expired with no attempts remaining'),
       lease_owner = null,
       lease_expires_at = null
 where state in ('leased', 'running')
   and lease_expires_at < now()
   and attempt >= max_attempts
   and not resume_without_attempt
returning id, run_id, source_id, attempt
"""


@dataclass
class ClaimedJob:
    """A job this worker holds a lease on."""

    id: UUID
    run_id: UUID
    source_id: str
    host: str
    attempt: int
    max_attempts: int
    requires: list[str]
    requires_any: list[str]
    params: dict[str, Any]
    proxy_snapshot: dict[str, Any]
    selected_browser_backend: BrowserBackendName | None = None
    trace_id: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any], params: dict[str, Any] | None = None) -> ClaimedJob:
        return cls(
            id=row["id"],
            run_id=row["run_id"],
            source_id=row["source_id"],
            host=row["host"],
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
            requires=list(row["requires"] or []),
            requires_any=list(row.get("requires_any") or []),
            params=params or {},
            proxy_snapshot=dict(row.get("proxy_snapshot") or {}),
            selected_browser_backend=(
                BrowserBackendName(row["selected_browser_backend"])
                if row.get("selected_browser_backend")
                else None
            ),
            trace_id=row.get("trace_id"),
        )


async def claim(
    connection: Connection, worker_id: UUID, capabilities: list[str], *, lease: int = LEASE_SECONDS
) -> ClaimedJob | None:
    """Take one job, or return None when there is nothing to take.

    One statement. `skip locked` is what makes this safe for several workers at
    once: each skips rows another is already deciding about rather than blocking
    behind it, so N workers claim N different jobs rather than serialising.
    """
    row = await _one(
        connection, CLAIM, {"worker": worker_id, "capabilities": capabilities, "lease": lease}
    )
    if row is None:
        return None

    job = ClaimedJob.from_row(row, await _run_params(connection, row["run_id"], row["source_id"]))
    if job.requires_any and job.selected_browser_backend is None:
        matches = sorted(set(job.requires_any).intersection(capabilities))
        browser_matches = [value for value in matches if value.startswith("browser:")]
        if not browser_matches:
            # The schema currently gives `requires_any` one purpose: browser
            # backend selection. Refuse an unknown OR-group shape before START
            # can consume an attempt without recording its choice.
            await release(connection, job, worker_id, delay=0, reason="unsupported any capability")
            return None
        job.selected_browser_backend = BrowserBackendName(
            browser_matches[0].removeprefix("browser:")
        )
    await events.emit(
        connection,
        events.Topic.JOB,
        "job.leased",
        run_id=job.run_id,
        job_id=job.id,
        worker_id=worker_id,
        source_id=job.source_id,
        payload={"attempt": job.attempt, "host": job.host},
    )
    return job


async def _run_params(connection: Connection, run_id: UUID, source_id: str) -> dict[str, Any]:
    """The run's parameters, with this source's overrides merged over them.

    A source override is the operational layer: "crawl this one slowly" or
    "never use the browser for this one" should not require editing the run that
    happens to contain it.
    """
    row = await _one(
        connection,
        """
        select coalesce(r.params, '{}'::jsonb) || coalesce(s.params, '{}'::jsonb) as params
          from catalogue.runs r
          left join catalogue.source_settings s on s.source_id = %(source)s
         where r.id = %(run)s
        """,
        {"run": run_id, "source": source_id},
    )
    return dict(row["params"]) if row and row["params"] else {}


async def start(
    connection: Connection,
    job: ClaimedJob,
    worker_id: UUID,
    *,
    trace_id: str | None = None,
    lease: int = LEASE_SECONDS,
) -> bool:
    """Move a claimed job to running and consume its attempt.

    Returns False when the lease was lost in between — which means another
    worker has legitimately taken this job, and this one must not run it too.
    """
    backend = _browser_backend_for_start(job)
    row = await _one(
        connection,
        START,
        {
            "id": job.id,
            "worker": worker_id,
            "trace": trace_id,
            "lease": lease,
            "backend": backend,
            "backend_capability": f"browser:{backend}" if backend else None,
        },
    )
    if row is None:
        LOGGER.warning("job.lease_lost", job_id=str(job.id), source=job.source_id)
        return False

    job.attempt = row["attempt"]
    job.selected_browser_backend = (
        BrowserBackendName(row["selected_browser_backend"])
        if row["selected_browser_backend"]
        else None
    )
    await events.emit(
        connection,
        events.Topic.JOB,
        "job.started",
        run_id=job.run_id,
        job_id=job.id,
        worker_id=worker_id,
        source_id=job.source_id,
        payload={
            "attempt": job.attempt,
            "max_attempts": job.max_attempts,
            "selected_browser_backend": job.selected_browser_backend,
        },
    )
    return True


def _browser_backend_for_start(job: ClaimedJob) -> BrowserBackendName | None:
    """Choose the deterministic backend represented by an any-of capability.

    Claiming already proved that the worker has one matching capability. The
    claimed row intentionally carries the intersection result indirectly: the
    worker capability chosen by the claim is recorded by callers on the job's
    selected field. For a new auto job, `claim` fills it from that intersection;
    an existing snapshot is preserved across retries.
    """
    return job.selected_browser_backend


async def renew(
    connection: Connection, worker_id: UUID, *, lease: int = LEASE_SECONDS
) -> list[dict[str, Any]]:
    """Extend every lease this worker holds, and report control flags.

    Renewal and control-flag polling are one statement because they happen at
    the same cadence and for the same reason: the heartbeat is the worker's only
    regular conversation with the database, and doing them separately would
    double the traffic to learn the same thing.
    """
    async with connection.cursor() as cursor:
        await cursor.execute(RENEW, {"worker": worker_id, "lease": lease})
        return await cursor.fetchall()


async def release(
    connection: Connection,
    job: ClaimedJob,
    worker_id: UUID,
    *,
    delay: int = HOST_BACKOFF_SECONDS,
    reason: str = "host busy",
) -> bool:
    """Put a job back on the queue without consuming an attempt.

    Used when a host slot could not be acquired, and when a worker draining on
    SIGTERM has to give back work it has not started. Neither is a failed
    attempt at the source, so neither may count as one.
    """
    row = await _one(connection, RELEASE, {"id": job.id, "worker": worker_id, "delay": delay})
    if row is None:
        return False
    await events.emit(
        connection,
        events.Topic.JOB,
        "job.released",
        run_id=job.run_id,
        job_id=job.id,
        worker_id=worker_id,
        source_id=job.source_id,
        payload={"reason": reason, "retry_in_seconds": delay},
    )
    LOGGER.info("job.released", source=job.source_id, reason=reason, retry_in=delay)
    return True


async def require_capability(
    connection: Connection,
    job: ClaimedJob,
    worker_id: UUID,
    capability: str,
    *,
    reason: str,
) -> bool:
    """Requeue a job with `capability` added to what a worker must advertise.

    For the case a static list cannot cover: whether a source needs a browser is
    decided by what its pages turn out to contain, not by which scraper it uses,
    so it can only be known once a page has been read. Discovering it mid-job is
    therefore normal rather than exceptional, and the job is rerouted rather
    than failed.

    Returns False when the job already carries the capability — meaning a worker
    that advertises it still could not serve it, and the caller must fail the
    job instead. This is what stops two workers bouncing an impossible job back
    and forth for ever, neither of them ever spending an attempt on it.
    """
    row = await _one(
        connection,
        ESCALATE,
        {"id": job.id, "worker": worker_id, "capability": capability},
    )
    if row is None:
        return False

    job.requires = list(row["requires"] or [])
    await events.emit(
        connection,
        events.Topic.JOB,
        "job.requeued",
        run_id=job.run_id,
        job_id=job.id,
        worker_id=worker_id,
        source_id=job.source_id,
        payload={"reason": reason, "requires": job.requires, "attempt": row["attempt"]},
    )
    LOGGER.info(
        "job.requeued", source=job.source_id, requires=job.requires, reason=reason
    )
    return True


async def reap_expired(connection: Connection) -> list[dict[str, Any]]:
    """Fail every expired job that has no attempts left.

    Jobs still below their limit need nothing done to them: the claim query
    selects expired leases directly, so they are picked up on the next tick
    without a separate transition through `queued`. Only the ones nothing will
    ever claim again have to be made terminal here, or they stay `running`
    forever with no process running them.
    """
    async with connection.cursor() as cursor:
        await cursor.execute(REAP)
        dead = await cursor.fetchall()

    for row in dead:
        await events.emit(
            connection,
            events.Topic.JOB,
            "job.failed",
            run_id=row["run_id"],
            job_id=row["id"],
            source_id=row["source_id"],
            payload={"reason": "lease expired", "attempt": row["attempt"]},
        )
        await events.notify(
            connection,
            "job.failed",
            f"{row['source_id']} exhausted its attempts",
            body="The worker holding it stopped reporting and no attempts remain.",
            run_id=row["run_id"],
            job_id=row["id"],
            source_id=row["source_id"],
        )
        LOGGER.warning("job.reaped", source=row["source_id"], job_id=str(row["id"]))

    # Slots held by a job that is now terminal, and slots whose own lease has
    # expired. Either way the shop is not being crawled and the slot is a lie.
    async with connection.cursor() as cursor:
        await cursor.execute(
            """
            update catalogue.host_leases
               set job_id = null, leased_by = null, leased_until = null
             where leased_until is not null and leased_until < now()
            """
        )

    return dead


async def cancel_requested(connection: Connection, job_id: UUID) -> dict[str, bool]:
    """The control flags for one job, read on the heartbeat."""
    row = await _one(
        connection,
        "select cancel_requested, pause_requested from catalogue.jobs where id = %(id)s",
        {"id": job_id},
    )
    if row is None:
        return {"cancel": False, "pause": False}
    return {"cancel": bool(row["cancel_requested"]), "pause": bool(row["pause_requested"])}


async def queue_depth(connection: Connection) -> dict[str, int]:
    """Jobs by state, for `/metrics` and the operations dashboard."""
    async with connection.cursor() as cursor:
        await cursor.execute(
            "select state, count(*) as n from catalogue.jobs "
            "where state not in ('succeeded','degraded','failed','cancelled','skipped') "
            "group by state"
        )
        return {row["state"]: int(row["n"]) for row in await cursor.fetchall()}


def lease_interval(seconds: int = LEASE_SECONDS) -> timedelta:
    return timedelta(seconds=seconds)


async def _one(connection: Connection, sql: str, params: Any = None) -> dict[str, Any] | None:
    async with connection.cursor() as cursor:
        await cursor.execute(sql, params)
        return await cursor.fetchone()
