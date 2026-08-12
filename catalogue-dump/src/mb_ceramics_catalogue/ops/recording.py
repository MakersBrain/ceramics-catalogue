"""Making a hand-run crawl leave the same record a scheduled one does.

Phase 2's whole visible result: `catalogue-dump --record` writes to
`catalogue.runs`, `jobs` and `job_progress`, so the crawl somebody typed at
09:00 shows up in the operations UI beside the one the scheduler started at
03:00. No behaviour changes — the same sources are collected the same way — but
the run stops being invisible the moment the terminal is closed.

Recording is **strictly optional and never fatal**. If the database is
unreachable, the crawl says so once and carries on collecting: losing 24,000
records because a bookkeeping table could not be written would be a much worse
outcome than an unrecorded run.
"""

from __future__ import annotations

import argparse
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from mb_ceramics_catalogue.config.settings import CrawlParams
from mb_ceramics_catalogue.config.sources import SourcesFile
from mb_ceramics_catalogue.observability import logging as obs
from mb_ceramics_catalogue.observability import tracing
from mb_ceramics_catalogue.ops import runs
from mb_ceramics_catalogue.ops.sink import PostgresSink

LOGGER = obs.get_logger("catalogue.recording")


@dataclass
class Recording:
    """The run this crawl is being recorded against, if any."""

    run_id: UUID | None = None
    jobs: dict[str, UUID] = field(default_factory=dict)
    sinks: list[Any] = field(default_factory=list)
    connection: Any = None

    @property
    def active(self) -> bool:
        return self.run_id is not None and self.connection is not None

    async def finish(self, outcomes: Sequence[Any]) -> None:
        """Put every job into a terminal state and let the run close itself."""
        if not self.active:
            return
        for outcome in outcomes:
            job_id = self.jobs.get(outcome.source)
            if job_id is None:
                continue
            summary = outcome.summary
            if outcome.interrupted:
                state = "cancelled"
            elif summary["error_count"] and not summary["records"]:
                state = "failed"
            else:
                # Errors alongside records is the normal state of a large
                # storefront: a handful of product pages 404 while the other
                # four thousand are fine. That is a succeeded job with a
                # non-zero error count, not a failure.
                state = "succeeded"
            try:
                await runs.finish_job(
                    self.connection,
                    job_id,
                    state=state,
                    summary=summary,
                    error=_first_error(summary) if state == "failed" else None,
                    artifact=_artifact_of(summary),
                )
            except Exception:
                LOGGER.warning("recording.finish_failed", source=outcome.source, exc_info=True)


def _first_error(summary: dict[str, Any]) -> str | None:
    errors = summary.get("errors") or []
    return str(errors[0].get("error"))[:2000] if errors else None


def _artifact_of(summary: dict[str, Any]) -> ArtifactRef | None:
    """The NDJSON this job wrote, as `catalogue.jobs` records it.

    Path, digest and size together: a recorded path with neither is a file that
    may or may not still hold what the run produced, which is not an audit
    trail. A dry run or a preserved existing file has no path and records none.
    """
    path = summary.get("artifact_path")
    if not path:
        return None
    return ArtifactRef(
        path=path,
        sha256=summary.get("artifact_sha256"),
        size=summary.get("artifact_size"),
    )


@dataclass(frozen=True)
class ArtifactRef:
    path: str
    sha256: str | None
    size: int | None


@asynccontextmanager
async def record_run(
    options: argparse.Namespace,
    params: CrawlParams,
    sources: SourcesFile,
    selected: list[str],
) -> AsyncIterator[Recording]:
    """Open a recording for this crawl, or an inert one when not asked for.

    The inert case is the default and costs nothing: no connection is opened, no
    sink is added, and the caller's code path is identical.
    """
    wanted = bool(options.record or options.run_id)
    if not wanted or params.dry_run:
        yield Recording()
        return

    # Imported here so `catalogue-dump` without --record needs no psycopg at
    # all, which is what lets the collection image skip the driver.
    from mb_ceramics_catalogue.storage import db

    try:
        dsn = db.dsn_from_environment(options.dsn)
    except ValueError as error:
        LOGGER.warning("recording.disabled", reason=str(error))
        yield Recording()
        return

    recording = Recording()
    try:
        async with db.connect(dsn) as connection:
            recording.connection = connection
            if options.run_id is not None:
                recording.run_id = options.run_id
                recording.jobs = await _existing_jobs(connection, options.run_id)
            else:
                recording.run_id = await runs.create_run(
                    connection,
                    kind="manual",
                    requested_by=_who(),
                    params=params.model_dump(mode="json"),
                )
                assert recording.run_id is not None
                recording.jobs = await runs.create_jobs(
                    connection, recording.run_id, sources, selected
                )

            await runs.start_run(connection, recording.run_id)
            for job_id in recording.jobs.values():
                await runs.record_job_start(connection, job_id, trace_id=tracing.trace_id())

            recording.sinks = [PostgresSink(connection, recording.run_id, recording.jobs)]
            obs.bind(run_id=str(recording.run_id))
            LOGGER.info(
                "recording.started", run_id=str(recording.run_id), jobs=len(recording.jobs)
            )
            yield recording
    except Exception:
        LOGGER.warning("recording.unavailable", exc_info=True)
        yield Recording()
    finally:
        obs.unbind("run_id")


async def _existing_jobs(connection: Any, run_id: UUID) -> dict[str, UUID]:
    async with connection.cursor() as cursor:
        await cursor.execute(
            "select source_id, id from catalogue.jobs where run_id = %(run)s", {"run": run_id}
        )
        return {row["source_id"]: row["id"] for row in await cursor.fetchall()}


def _who() -> str:
    """Who asked for this run, for `catalogue.runs.requested_by`.

    Best effort and never fatal: this is a label in a list, not an identity
    claim, and the API sets it properly from the authenticated session.
    """
    import getpass
    import socket

    try:
        return f"{getpass.getuser()}@{socket.gethostname()}"
    except Exception:  # noqa: BLE001 - a container may have neither
        return "cli"
