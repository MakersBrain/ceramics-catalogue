"""Edges, log lines and notifications: the three durable things a run leaves.

The vocabulary is one vocabulary. An event named `job.failed` appears with that
name on stdout, in `catalogue.job_events`, in `catalogue.event_log` and as the
SSE `event:` field a browser listens for. Anything else means an operator has to
learn a translation table between four views of the same run.

The split this module exists to enforce (§3.1):

* `emit()` writes an **edge** to `catalogue.event_log`. Discrete, ordered by one
  bigint sequence, replayable from `Last-Event-ID`. Bad to miss.
* `log()` writes a **line** to `catalogue.job_events`. Per job, pruned at 30
  days, read by the job detail page.
* Levels — `job_progress`, `workers.last_heartbeat_at` — are written in place by
  their own modules and never appear here. Putting progress in the event log
  would make it ~860,000 rows per run and break replay entirely, which is the
  kind of change that looks tidy in review and is not.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from mb_ceramics_catalogue.observability import logging as obs

LOGGER = obs.get_logger("catalogue.events")

Connection = psycopg.AsyncConnection[dict[str, Any]]


class Topic(StrEnum):
    RUN = "run"
    JOB = "job"
    WORKER = "worker"
    NOTIFICATION = "notification"
    SCHEDULE = "schedule"
    SOURCE = "source"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


INSERT_EVENT = """
insert into catalogue.event_log (topic, type, run_id, job_id, worker_id, source_id, payload)
values (%(topic)s, %(type)s, %(run_id)s, %(job_id)s, %(worker_id)s, %(source_id)s, %(payload)s)
returning id, at
"""

INSERT_JOB_EVENT = """
insert into catalogue.job_events (job_id, level, event, message, data)
values (%(job_id)s, %(level)s, %(event)s, %(message)s, %(data)s)
returning id
"""


async def emit(
    connection: Connection,
    topic: Topic | str,
    event_type: str,
    *,
    run_id: UUID | None = None,
    job_id: UUID | None = None,
    worker_id: UUID | None = None,
    source_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    """Record one edge and return its id.

    The insert trigger issues `notify catalogue_ops, '<id>'` — the id only. A
    payload carrying `in_flight` would exceed the 8000-byte `notify` cap and
    fail the insert, and `notify` is fire-and-forget so a listener that is
    reconnecting loses it permanently. Carrying the id makes the notification a
    hint to go and read, and the table stays the authority: a missed hint costs
    latency, never data.
    """
    row = await _fetch_one(
        connection,
        INSERT_EVENT,
        {
            "topic": str(topic),
            "type": event_type,
            "run_id": run_id,
            "job_id": job_id,
            "worker_id": worker_id,
            "source_id": source_id,
            "payload": Jsonb(payload or {}),
        },
    )
    assert row is not None
    return int(row["id"])


async def log(
    connection: Connection,
    job_id: UUID,
    message: str,
    *,
    level: str = "info",
    event: str | None = None,
    data: dict[str, Any] | None = None,
) -> int:
    """Append one line to a job's log."""
    row = await _fetch_one(
        connection,
        INSERT_JOB_EVENT,
        {
            "job_id": job_id,
            "level": level,
            "event": event,
            "message": message,
            "data": Jsonb(data) if data else None,
        },
    )
    assert row is not None
    return int(row["id"])


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

RAISE_NOTIFICATION = """
insert into catalogue.notifications
       (severity, kind, title, body, run_id, job_id, source_id, worker_id, dedup_key)
values (%(severity)s, %(kind)s, %(title)s, %(body)s, %(run_id)s, %(job_id)s,
        %(source_id)s, %(worker_id)s, %(dedup_key)s)
-- The partial unique index covers only unresolved, unacknowledged rows, so this
-- collides exactly when the same condition is already open and untouched.
-- Three retries of one source at 03:00 are one notification, not three.
on conflict do nothing
returning id
"""

RESOLVE_NOTIFICATION = """
update catalogue.notifications
   set resolved_at = now()
 where dedup_key = %(dedup_key)s
   and resolved_at is null
returning id
"""

ACKNOWLEDGE_NOTIFICATION = """
update catalogue.notifications
   set acknowledged_at = now(), acknowledged_by = %(by)s
 where id = %(id)s and acknowledged_at is null
returning id
"""


async def notify(
    connection: Connection,
    kind: str,
    title: str,
    *,
    severity: Severity | str = Severity.WARNING,
    dedup_key: str | None = None,
    body: str | None = None,
    run_id: UUID | None = None,
    job_id: UUID | None = None,
    source_id: str | None = None,
    worker_id: UUID | None = None,
) -> int | None:
    """Raise a notification unless the same condition is already open.

    Returns the new id, or None when it was deduplicated — which is the normal
    case for a source that has been stale for a week and is worth exactly one
    unresolved row rather than seven.

    An edge is emitted alongside, so a browser that is open sees it arrive
    rather than finding it on the next poll.
    """
    key = dedup_key or ":".join(filter(None, [kind, source_id, str(job_id or "")]))
    row = await _fetch_one(
        connection,
        RAISE_NOTIFICATION,
        {
            "severity": str(severity),
            "kind": kind,
            "title": title,
            "body": body,
            "run_id": run_id,
            "job_id": job_id,
            "source_id": source_id,
            "worker_id": worker_id,
            "dedup_key": key,
        },
    )
    if row is None:
        LOGGER.debug("notification.deduplicated", kind=kind, dedup_key=key)
        return None

    notification_id = int(row["id"])
    await emit(
        connection,
        Topic.NOTIFICATION,
        "notification.raised",
        run_id=run_id,
        job_id=job_id,
        worker_id=worker_id,
        source_id=source_id,
        payload={
            "id": notification_id,
            "severity": str(severity),
            "kind": kind,
            "title": title,
        },
    )
    LOGGER.warning("notification.raised", kind=kind, title=title, severity=str(severity))
    return notification_id


async def resolve(connection: Connection, dedup_key: str, *, source_id: str | None = None) -> bool:
    """Close a condition that has ended.

    `source.stale` clears when the source succeeds. Without this a warning
    raised once stays on the operator's screen for ever and the feed stops
    meaning anything.
    """
    row = await _fetch_one(connection, RESOLVE_NOTIFICATION, {"dedup_key": dedup_key})
    if row is None:
        return False
    await emit(
        connection,
        Topic.NOTIFICATION,
        "notification.resolved",
        source_id=source_id,
        payload={"id": int(row["id"]), "dedup_key": dedup_key},
    )
    return True


async def acknowledge(connection: Connection, notification_id: int, by: str) -> bool:
    row = await _fetch_one(
        connection, ACKNOWLEDGE_NOTIFICATION, {"id": notification_id, "by": by}
    )
    if row is None:
        return False
    await emit(
        connection,
        Topic.NOTIFICATION,
        "notification.acknowledged",
        payload={"id": notification_id, "by": by},
    )
    return True


async def _fetch_one(
    connection: Connection, sql: str, params: dict[str, Any]
) -> dict[str, Any] | None:
    async with connection.cursor() as cursor:
        await cursor.execute(sql, params)
        return await cursor.fetchone()
