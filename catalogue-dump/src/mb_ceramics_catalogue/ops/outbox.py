"""Provider-neutral transactional job publication and recovery."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import psycopg

from mb_ceramics_catalogue.ops.delivery import SCHEMA, JobEnvelope

Connection = psycopg.AsyncConnection[dict[str, Any]]


def route_for(
    requires: list[str],
    requires_any: list[str],
    selected_browser_backend: str | None,
) -> str:
    """Return the single disjoint delivery route for a job snapshot."""
    if selected_browser_backend:
        return f"browser.{selected_browser_backend}.normal"
    if requires_any:
        return "browser.auto.normal"
    if "browser" in requires:
        return "browser.camoufox.normal"
    return "plain.normal"


async def enqueue_job(connection: Connection, job_id: UUID, *, available_at: Any = None) -> bool:
    """Insert one generation into the outbox in the caller's transaction."""
    row = await _one(
        connection,
        """
        insert into catalogue.queue_outbox
                    (job_id, generation, route, envelope_schema,
                     deduplication_key, payload, available_at)
        select j.id, j.delivery_generation, %(route)s::text, %(schema)s::text,
               j.id::text || ':' || j.delivery_generation::text,
               jsonb_build_object(
                 'schema', %(schema)s::text,
                 'job_id', j.id::text,
                 'run_id', j.run_id::text,
                 'source_id', j.source_id,
                 'generation', j.delivery_generation,
                 'route', %(route)s::text,
                 'priority', j.priority,
                 'enqueued_at', to_char(now() at time zone 'UTC',
                                        'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"')
               ),
               coalesce(%(available_at)s, j.scheduled_for)
          from catalogue.jobs j
         where j.id = %(job)s and j.state not in ('succeeded', 'degraded', 'failed',
                                                   'cancelled', 'skipped')
        on conflict (job_id, generation) do nothing
        returning id
        """,
        {
            "job": job_id,
            "schema": SCHEMA,
            "route": await job_route(connection, job_id),
            "available_at": available_at,
        },
    )
    return row is not None


async def job_route(connection: Connection, job_id: UUID) -> str:
    row = await _one(
        connection,
        "select requires, requires_any, selected_browser_backend from catalogue.jobs where id = %(id)s",
        {"id": job_id},
    )
    if row is None:
        raise ValueError(f"unknown job {job_id}")
    return route_for(
        list(row["requires"] or []),
        list(row["requires_any"] or []),
        row["selected_browser_backend"],
    )


async def pending(connection: Connection, *, limit: int = 100) -> list[dict[str, Any]]:
    """Lock a publication batch; callers must mark it before committing."""
    async with connection.cursor() as cursor:
        await cursor.execute(
            """
            select id, job_id, generation, route, envelope_schema,
                   deduplication_key, payload
              from catalogue.queue_outbox
             where published_at is null and cancelled_at is null and available_at <= now()
             order by available_at, id
             for update skip locked
             limit %(limit)s
            """,
            {"limit": limit},
        )
        return await cursor.fetchall()


async def mark_published(connection: Connection, outbox_id: int) -> None:
    await connection.execute(
        "update catalogue.queue_outbox set published_at = now(), publish_attempts = publish_attempts + 1, "
        "last_error = null where id = %(id)s and published_at is null",
        {"id": outbox_id},
    )


async def mark_failed(connection: Connection, outbox_id: int, error: str) -> None:
    await connection.execute(
        "update catalogue.queue_outbox set publish_attempts = publish_attempts + 1, "
        "last_error = %(error)s, available_at = now() + interval '5 seconds' "
        "where id = %(id)s and published_at is null",
        {"id": outbox_id, "error": error[:2000]},
    )


def envelope(row: dict[str, Any]) -> JobEnvelope:
    raw = dict(row["payload"])
    return JobEnvelope(
        job_id=UUID(raw["job_id"]),
        run_id=UUID(raw["run_id"]),
        source_id=str(raw["source_id"]),
        generation=int(raw["generation"]),
        route=str(raw["route"]),
        priority=int(raw["priority"]),
        enqueued_at=datetime.fromisoformat(raw["enqueued_at"]).astimezone(UTC),
    )


async def reconstruct_missing(connection: Connection) -> int:
    """Create outbox rows for eligible generations missing after an outage."""
    cursor = await connection.execute(
        """
        select j.id
          from catalogue.jobs j
          left join catalogue.queue_outbox o
            on o.job_id = j.id and o.generation = j.delivery_generation
          left join catalogue.source_settings s on s.source_id = j.source_id
         where j.state in ('queued', 'leased', 'running')
           and o.id is null
           and not j.cancel_requested and not j.pause_requested
           and coalesce(s.enabled, true) and not coalesce(s.paused, false)
        """
    )
    rows = await cursor.fetchall()
    for row in rows:
        await enqueue_job(connection, row["id"])
    return len(rows)


async def republish_current(connection: Connection) -> int:
    """Re-offer every current eligible generation after broker-state loss."""
    await reconstruct_missing(connection)
    cursor = await connection.execute(
        """
        update catalogue.queue_outbox o
           set published_at = null, cancelled_at = null, available_at = now(), last_error = null
          from catalogue.jobs j
          left join catalogue.source_settings s on s.source_id = j.source_id
         where o.job_id = j.id and o.generation = j.delivery_generation
           and j.state in ('queued', 'leased', 'running')
           and not j.cancel_requested and not j.pause_requested
           and coalesce(s.enabled, true) and not coalesce(s.paused, false)
        returning o.id
        """
    )
    return len(await cursor.fetchall())


async def redrive_exhausted(connection: Connection, exhausted: JobEnvelope) -> bool:
    """Create a new generation when the provider exhausted the current one."""
    row = await _one(
        connection,
        """
        select j.id, j.delivery_generation, j.state, j.cancel_requested, j.pause_requested,
               coalesce(s.enabled, true) enabled, coalesce(s.paused, false) source_paused
          from catalogue.jobs j
          left join catalogue.source_settings s on s.source_id = j.source_id
         where j.id = %(id)s
         for update of j
        """,
        {"id": exhausted.job_id},
    )
    if (
        row is None
        or int(row["delivery_generation"]) != exhausted.generation
        or row["state"] not in ("queued", "leased", "running")
        or row["cancel_requested"]
        or row["pause_requested"]
        or not row["enabled"]
        or row["source_paused"]
    ):
        return False
    await connection.execute(
        "update catalogue.jobs set delivery_generation = delivery_generation + 1 where id = %(id)s",
        {"id": exhausted.job_id},
    )
    return await enqueue_job(connection, exhausted.job_id)


async def _one(connection: Connection, statement: str, params: dict[str, Any]) -> dict[str, Any] | None:
    async with connection.cursor() as cursor:
        await cursor.execute(statement, params)
        return await cursor.fetchone()
