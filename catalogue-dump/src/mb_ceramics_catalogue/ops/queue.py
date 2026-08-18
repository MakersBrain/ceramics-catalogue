"""PostgreSQL job state and fencing for NATS-delivered work.

Workers never scan this table for work. They reserve the exact generation named
by a JetStream message, and every execution-owned mutation compares a random
token so stale or duplicate deliveries are harmless.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

import psycopg

from mb_ceramics_catalogue.connectors import BrowserBackendName
from mb_ceramics_catalogue.observability import logging as obs
from mb_ceramics_catalogue.observability import metrics
from mb_ceramics_catalogue.ops import events, outbox, runs
from mb_ceramics_catalogue.ops.job_queue import JobEnvelope

LOGGER = obs.get_logger("catalogue.queue")
Connection = psycopg.AsyncConnection[dict[str, Any]]
LEASE_SECONDS = 300
HOST_BACKOFF_SECONDS = 30
TERMINAL = ("succeeded", "degraded", "failed", "cancelled", "skipped")


@dataclass
class ClaimedJob:
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
    delivery_generation: int
    execution_token: UUID
    selected_browser_backend: BrowserBackendName | None = None
    trace_id: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> ClaimedJob:
        return cls(
            id=row["id"], run_id=row["run_id"], source_id=row["source_id"], host=row["host"],
            attempt=row["attempt"], max_attempts=row["max_attempts"],
            requires=list(row["requires"] or []), requires_any=list(row["requires_any"] or []),
            params=dict(row["params"] or {}), proxy_snapshot=dict(row["proxy_snapshot"] or {}),
            delivery_generation=int(row["delivery_generation"]),
            execution_token=row["execution_token"], trace_id=row.get("trace_id"),
            selected_browser_backend=(BrowserBackendName(row["selected_browser_backend"])
                                      if row.get("selected_browser_backend") else None),
        )


@dataclass(frozen=True)
class Reservation:
    job: ClaimedJob | None
    disposition: Literal["run", "ack", "retry"]
    retry_after: float = 0.0


async def reserve(
    connection: Connection,
    envelope: JobEnvelope,
    worker_id: UUID,
    capabilities: list[str],
    *,
    lease: int = LEASE_SECONDS,
) -> Reservation:
    """Reserve precisely the broker-delivered job generation."""
    async with connection.transaction():
        await _execution_lock(connection, envelope.job_id)
        row = await _one(
            connection,
            """
            select j.*, coalesce(r.params, '{}'::jsonb) || coalesce(s.params, '{}'::jsonb) params,
                   coalesce(s.enabled, true) source_enabled,
                   coalesce(s.paused, false) source_paused
              from catalogue.jobs j
              join catalogue.runs r on r.id = j.run_id
              left join catalogue.source_settings s on s.source_id = j.source_id
             where j.id = %(id)s
             for update of j
            """,
            {"id": envelope.job_id},
        )
        if row is None or int(row["delivery_generation"]) != envelope.generation:
            metrics.REGISTRY.counter(
                "catalogue_queue_stale_deliveries_total",
                "Deliveries ACKed because their job generation was stale or absent.",
                route=envelope.route,
            )
            return Reservation(None, "ack")
        if row["state"] in TERMINAL:
            metrics.REGISTRY.counter(
                "catalogue_queue_terminal_redeliveries_total",
                "Deliveries ACKed because the authoritative job was terminal.",
                state=row["state"],
            )
            return Reservation(None, "ack")
        if not row["source_enabled"] or row["cancel_requested"]:
            state = "skipped" if not row["source_enabled"] else "cancelled"
            await connection.execute(
                "update catalogue.jobs set state = %(state)s, finished_at = now(), "
                "lease_owner = null, lease_expires_at = null, execution_token = null "
                "where id = %(id)s",
                {"id": envelope.job_id, "state": state},
            )
            await _emit_terminal(connection, row, state, "source disabled" if state == "skipped" else "cancelled")
            await runs.close_run_if_done(connection, row["run_id"])
            return Reservation(None, "ack")
        if row["source_paused"] or row["pause_requested"] or row["state"] == "paused":
            return Reservation(None, "ack")
        scheduled_for = row["scheduled_for"]
        now = await _database_now(connection)
        if scheduled_for > now:
            return Reservation(None, "retry", (scheduled_for - now).total_seconds())
        if row["state"] in ("leased", "running") and row["lease_expires_at"] > now:
            metrics.REGISTRY.counter(
                "catalogue_queue_execution_conflicts_total",
                "Duplicate deliveries delayed behind a live execution token.",
                route=envelope.route,
            )
            return Reservation(None, "retry", min((row["lease_expires_at"] - now).total_seconds(), 30.0))
        if row["attempt"] >= row["max_attempts"] and not row["resume_without_attempt"]:
            await connection.execute(
                "update catalogue.jobs set state = 'failed', finished_at = now(), "
                "error = coalesce(error, 'lease expired with no attempts remaining'), "
                "lease_owner = null, lease_expires_at = null, execution_token = null where id = %(id)s",
                {"id": envelope.job_id},
            )
            await _emit_terminal(connection, row, "failed", "attempts exhausted")
            return Reservation(None, "ack")

        available = set(capabilities)
        if not set(row["requires"] or []).issubset(available):
            return Reservation(None, "retry", 30.0)

        selected = row["selected_browser_backend"]
        if envelope.route == "browser.auto.normal" and not selected:
            matches = sorted(
                value.removeprefix("browser:") for value in capabilities
                if value.startswith("browser:") and value in set(row["requires_any"] or [])
            )
            if not matches:
                return Reservation(None, "ack")
            selected = matches[0]
        elif envelope.route.startswith("browser.") and envelope.route != "browser.auto.normal":
            routed_backend = envelope.route.removeprefix("browser.").removesuffix(".normal")
            if selected and selected != routed_backend:
                return Reservation(None, "ack")
            selected = selected or routed_backend
        expected_route = outbox.route_for(
            list(row["requires"] or []), list(row["requires_any"] or []), selected
        )
        if selected and f"browser:{selected}" not in available:
            return Reservation(None, "retry", 30.0)
        if expected_route != envelope.route:
            await connection.execute(
                "update catalogue.jobs set selected_browser_backend = %(backend)s, "
                "delivery_generation = delivery_generation + 1 where id = %(id)s",
                {"id": envelope.job_id, "backend": selected},
            )
            await outbox.enqueue_job(connection, envelope.job_id)
            metrics.REGISTRY.counter(
                "catalogue_queue_browser_reroutes_total",
                "Auto-browser deliveries republished to an exact backend.",
                backend=str(selected),
            )
            return Reservation(None, "ack")

        token = uuid4()
        claimed = await _one(
            connection,
            """
            update catalogue.jobs
               set state = 'leased', lease_owner = %(worker)s, execution_token = %(token)s,
                   selected_browser_backend = coalesce(selected_browser_backend, %(backend)s),
                   lease_expires_at = now() + make_interval(secs => %(lease)s)
             where id = %(id)s and delivery_generation = %(generation)s
            returning *
            """,
            {"id": envelope.job_id, "generation": envelope.generation, "worker": worker_id,
             "token": token, "backend": selected, "lease": lease},
        )
        assert claimed is not None
        claimed["params"] = row["params"]
        job = ClaimedJob.from_row(claimed)
        await events.emit(
            connection, events.Topic.JOB, "job.leased", run_id=job.run_id, job_id=job.id,
            worker_id=worker_id, source_id=job.source_id,
            payload={"attempt": job.attempt, "host": job.host, "generation": envelope.generation},
        )
        return Reservation(job, "run")


async def start(
    connection: Connection, job: ClaimedJob, worker_id: UUID, *,
    trace_id: str | None = None, lease: int = LEASE_SECONDS,
) -> bool:
    row = await _one(
        connection,
        """
        update catalogue.jobs
           set state = 'running', attempt = attempt + case when resume_without_attempt then 0 else 1 end,
               resume_without_attempt = false, started_at = coalesce(started_at, now()),
               trace_id = coalesce(%(trace)s, trace_id),
               lease_expires_at = now() + make_interval(secs => %(lease)s)
         where id = %(id)s and delivery_generation = %(generation)s
           and lease_owner = %(worker)s and execution_token = %(token)s
           and state = 'leased' and lease_expires_at > now()
           and not cancel_requested and not pause_requested
        returning attempt, max_attempts, selected_browser_backend
        """,
        {"id": job.id, "generation": job.delivery_generation, "worker": worker_id,
         "token": job.execution_token, "trace": trace_id, "lease": lease},
    )
    if row is None:
        LOGGER.warning("job.lease_lost", job_id=str(job.id), source=job.source_id)
        return False
    job.attempt = row["attempt"]
    await events.emit(
        connection, events.Topic.JOB, "job.started", run_id=job.run_id, job_id=job.id,
        worker_id=worker_id, source_id=job.source_id,
        payload={"attempt": job.attempt, "max_attempts": job.max_attempts,
                 "selected_browser_backend": job.selected_browser_backend},
    )
    return True


async def renew(
    connection: Connection, jobs: list[ClaimedJob], worker_id: UUID, *, lease: int = LEASE_SECONDS
) -> list[dict[str, Any]]:
    held: list[dict[str, Any]] = []
    for job in jobs:
        row = await _one(
            connection,
            "update catalogue.jobs set lease_expires_at = now() + make_interval(secs => %(lease)s) "
            "where id = %(id)s and lease_owner = %(worker)s and execution_token = %(token)s "
            "and state in ('leased','running') returning id, cancel_requested, pause_requested",
            {"id": job.id, "worker": worker_id, "token": job.execution_token, "lease": lease},
        )
        if row:
            held.append(row)
    return held


async def release(
    connection: Connection, job: ClaimedJob, worker_id: UUID, *,
    delay: int = HOST_BACKOFF_SECONDS, reason: str = "host busy",
) -> bool:
    row = await _one(
        connection,
        "update catalogue.jobs set state = 'queued', lease_owner = null, lease_expires_at = null, "
        "execution_token = null, scheduled_for = now() + make_interval(secs => %(delay)s) "
        "where id = %(id)s and lease_owner = %(worker)s and execution_token = %(token)s "
        "returning id",
        {"id": job.id, "worker": worker_id, "token": job.execution_token, "delay": delay},
    )
    if row is None:
        return False
    await events.emit(
        connection, events.Topic.JOB, "job.released", run_id=job.run_id, job_id=job.id,
        worker_id=worker_id, source_id=job.source_id,
        payload={"reason": reason, "retry_in_seconds": delay},
    )
    return True


async def require_capability(
    connection: Connection, job: ClaimedJob, worker_id: UUID, capability: str, *, reason: str
) -> bool:
    async with connection.transaction():
        row = await _one(
            connection,
            """
            update catalogue.jobs
               set state = 'queued', requires = (select array(select distinct unnest(
                     requires || array[%(capability)s]::text[]) order by 1)),
                   requires_any = case when %(capability)s = 'browser'
                     then array['browser:camoufox','browser:cdp_extension_proxy'] else requires_any end,
                   selected_browser_backend = null, resume_without_attempt = true,
                   lease_owner = null, lease_expires_at = null, execution_token = null,
                   delivery_generation = delivery_generation + 1
             where id = %(id)s and lease_owner = %(worker)s and execution_token = %(token)s
               and not (%(capability)s = any(requires))
            returning delivery_generation, requires, attempt
            """,
            {"id": job.id, "worker": worker_id, "token": job.execution_token,
             "capability": capability},
        )
        if row is None:
            return False
        await outbox.enqueue_job(connection, job.id)
        await events.emit(
            connection, events.Topic.JOB, "job.requeued", run_id=job.run_id, job_id=job.id,
            worker_id=worker_id, source_id=job.source_id,
            payload={"reason": reason, "requires": row["requires"], "attempt": row["attempt"]},
        )
        return True


async def reconcile(connection: Connection) -> int:
    """Repair expired executions; discovery remains exclusively broker-driven."""
    cursor = await connection.execute(
        "select id from catalogue.jobs where state in ('leased','running') "
        "and lease_expires_at < now() order by lease_expires_at"
    )
    candidates = [row["id"] for row in await cursor.fetchall()]
    rows: list[dict[str, Any]] = []
    for job_id in candidates:
        await _execution_lock(connection, job_id)
        row = await _one(
            connection,
        """
        update catalogue.jobs
           set state = case when attempt >= max_attempts and not resume_without_attempt
                            then 'failed' else 'queued' end,
               finished_at = case when attempt >= max_attempts and not resume_without_attempt
                                  then now() else finished_at end,
               error = case when attempt >= max_attempts and not resume_without_attempt
                            then coalesce(error, 'lease expired with no attempts remaining') else error end,
               lease_owner = null, lease_expires_at = null, execution_token = null
         where id = %(id)s and state in ('leased','running') and lease_expires_at < now()
        returning id, run_id, source_id, state, attempt
        """,
            {"id": job_id},
        )
        if row is not None:
            rows.append(row)
    await connection.execute(
        "update catalogue.host_leases set job_id = null, leased_by = null, leased_until = null, "
        "execution_token = null where leased_until < now()"
    )
    for row in rows:
        metrics.REGISTRY.counter(
            "catalogue_queue_expired_executions_total",
            "Expired PostgreSQL executions reconciled.",
            outcome=row["state"],
        )
        if row["state"] == "failed":
            await _emit_terminal(connection, row, "failed", "lease expired")
            await runs.close_run_if_done(connection, row["run_id"])
    return len(rows)


async def cancel_requested(connection: Connection, job_id: UUID) -> dict[str, bool]:
    row = await _one(connection, "select cancel_requested, pause_requested from catalogue.jobs "
                     "where id = %(id)s", {"id": job_id})
    return {"cancel": bool(row and row["cancel_requested"]),
            "pause": bool(row and row["pause_requested"])}


async def queue_depth(connection: Connection) -> dict[str, int]:
    cursor = await connection.execute(
        "select state, count(*) n from catalogue.jobs where state not in "
        "('succeeded','degraded','failed','cancelled','skipped') group by state"
    )
    return {row["state"]: int(row["n"]) for row in await cursor.fetchall()}


def lease_interval(seconds: int = LEASE_SECONDS) -> timedelta:
    return timedelta(seconds=seconds)


async def _database_now(connection: Connection) -> Any:
    row = await _one(connection, "select now() value")
    assert row is not None
    return row["value"]


async def _execution_lock(connection: Connection, job_id: UUID) -> None:
    """Serialize token replacement with material job-owned writes."""
    await connection.execute(
        "select pg_advisory_xact_lock(hashtextextended(%(id)s::text, 0))", {"id": job_id}
    )


async def _emit_terminal(
    connection: Connection, row: dict[str, Any], state: str, reason: str
) -> None:
    await events.emit(connection, events.Topic.JOB, f"job.{state}", run_id=row["run_id"],
                      job_id=row["id"], source_id=row["source_id"],
                      payload={"reason": reason, "attempt": row["attempt"]})


async def _one(connection: Connection, sql: str, params: Any = None) -> dict[str, Any] | None:
    async with connection.cursor() as cursor:
        await cursor.execute(sql, params)
        return await cursor.fetchone()
