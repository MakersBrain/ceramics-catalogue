"""Politeness across workers: at most N requests to one shop, whoever is asking.

This is the one genuinely new correctness problem the worker introduces.
`HostLimiter` bounds concurrency inside a process; three worker containers would
triple the load on every shop, and getting a source blocked costs more than any
feature in this plan is worth.

A slot is a row. Before running, a worker locks the host row and claims one free
or expired slot numbered `1..max_concurrency`; if it cannot, the job goes back on
the queue with **no attempt consumed**, because being polite is not a failed
attempt at the source.

The default is one slot per host, which is right: jobs are per source, and
several sources share a host only where a manufacturer and its shop are the same
site. Per-host overrides live in `catalogue.hosts` so they can be tuned without
a deploy.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mb_ceramics_catalogue.observability import logging as obs
from mb_ceramics_catalogue.observability import metrics

LOGGER = obs.get_logger("catalogue.leases")

Connection = psycopg.AsyncConnection[dict[str, Any]]

#: How long a host slot is held without renewal. Shorter than the job lease, so
#: a slot is freed for another worker before the job itself is considered lost.
SLOT_SECONDS = 180

ACQUIRE = """
with free as (
  select host, slot
    from catalogue.host_leases
   where host = %(host)s
     -- Free, or held by something that stopped renewing. A worker that died
     -- mid-request leaves a slot that must not be occupied for ever.
     and (job_id is null or leased_until is null or leased_until < now())
   order by slot
   for update skip locked
   limit 1
)
update catalogue.host_leases l
   set job_id = %(job)s,
       leased_by = %(worker)s,
       leased_until = now() + make_interval(secs => %(seconds)s)
  from free
 where l.host = free.host and l.slot = free.slot
returning l.slot
"""

RELEASE = """
update catalogue.host_leases
   set job_id = null, leased_by = null, leased_until = null
 where host = %(host)s and job_id = %(job)s
returning slot
"""

RELEASE_ALL = """
update catalogue.host_leases
   set job_id = null, leased_by = null, leased_until = null
 where job_id = %(job)s
returning host, slot
"""

RENEW = """
update catalogue.host_leases
   set leased_until = now() + make_interval(secs => %(seconds)s)
 where leased_by = %(worker)s and job_id is not null
returning host, slot
"""


async def acquire(
    connection: Connection,
    host: str,
    job_id: UUID,
    worker_id: UUID,
    *,
    seconds: int = SLOT_SECONDS,
) -> int | None:
    """Take one of this host's slots, or return None when they are all busy.

    The host's slots are reconciled first, inside the same transaction and while
    holding the host row lock, so a host nobody has crawled before gets its
    default slot created rather than being permanently unclaimable.
    """
    async with connection.transaction(), connection.cursor() as cursor:
        await cursor.execute(
            "select catalogue.reconcile_host_slots(%(host)s)", {"host": host}
        )
        await cursor.execute(
            ACQUIRE,
            {"host": host, "job": job_id, "worker": worker_id, "seconds": seconds},
        )
        row = await cursor.fetchone()

    if row is None:
        metrics.REGISTRY.counter(
            "catalogue_host_lease_contended_total",
            "Times a job waited because its host had no free slot.",
            host=host,
        )
        LOGGER.info("host.lease.contended", host=host, job_id=str(job_id))
        return None

    LOGGER.debug("host.lease.acquired", host=host, slot=row["slot"])
    return int(row["slot"])


async def release(connection: Connection, host: str, job_id: UUID) -> bool:
    """Give a host slot back. Safe to call when none is held."""
    async with connection.cursor() as cursor:
        await cursor.execute(RELEASE, {"host": host, "job": job_id})
        row = await cursor.fetchone()
    if row is not None:
        LOGGER.debug("host.lease.released", host=host, slot=row["slot"])
    return row is not None


async def release_all(connection: Connection, job_id: UUID) -> list[str]:
    """Give back every slot this job holds, whichever key it holds them under.

    A job takes one slot per politeness key — its shop, and the shared edge that
    shop sits behind, if any — so releasing by hostname alone would strand the
    other. The job id is what they have in common, and it is unique to one job,
    so asking by it needs no caller to remember what was taken.
    """
    async with connection.cursor() as cursor:
        await cursor.execute(RELEASE_ALL, {"job": job_id})
        rows = await cursor.fetchall()
    for row in rows:
        LOGGER.debug("host.lease.released", host=row["host"], slot=row["slot"])
    return [str(row["host"]) for row in rows]


async def renew(connection: Connection, worker_id: UUID, *, seconds: int = SLOT_SECONDS) -> int:
    """Extend every slot this worker holds. Called with the job heartbeat."""
    async with connection.cursor() as cursor:
        await cursor.execute(RENEW, {"worker": worker_id, "seconds": seconds})
        return len(await cursor.fetchall())


async def configure(connection: Connection, host: str, max_concurrency: int) -> None:
    """Set a host's bound and reconcile its slots.

    Raising the limit creates the new slots. Lowering it removes only idle ones:
    taking a slot away from a running job does not stop the requests it is
    already making, so the row survives until its lease ends.
    """
    async with connection.transaction(), connection.cursor() as cursor:
        await cursor.execute(
            "insert into catalogue.hosts (host, max_concurrency) values (%(host)s, %(n)s) "
            "on conflict (host) do update set max_concurrency = excluded.max_concurrency",
            {"host": host, "n": max_concurrency},
        )
        await cursor.execute("select catalogue.reconcile_host_slots(%(host)s)", {"host": host})
    LOGGER.info("host.configured", host=host, max_concurrency=max_concurrency)


async def held_by(connection: Connection, worker_id: UUID) -> list[dict[str, Any]]:
    async with connection.cursor() as cursor:
        await cursor.execute(
            "select host, slot, job_id from catalogue.host_leases where leased_by = %(worker)s",
            {"worker": worker_id},
        )
        return await cursor.fetchall()
