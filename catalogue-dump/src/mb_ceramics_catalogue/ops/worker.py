"""The worker: claim a source, crawl it, load it, report it, repeat.

This is what turns the scrape and the load from two manual steps joined by files
on disk into one automatic step, and what makes "start a run" mean "insert a
row" rather than "have a terminal open".

The conventions deliberately mirror `ateliera-app/apps/api/src/workers/
lifecycle.ts`: the same event vocabulary (`worker.starting`, `worker.ready`,
`worker.tick`, `worker.stopping`), the same heartbeat-and-backoff shape. An
operator who knows one knows the other.

The loop, in order, and each step is in this order for a reason:

    register                       durable, so a dead worker is a stale
                                   heartbeat rather than an absence
    heartbeat every 5s             in its own task on its own connection, so a
                                   busy job cannot block the liveness signal
    loop:
      observe desired_state        claim only while it is running
      reap expired leases          recover work from workers that died
      claim a job                  skip locked, honouring capabilities
      acquire a host slot          else release with a short backoff, no
                                   attempt burnt
      mark running                 and consume the attempt, not before
      crawl                        the existing run_source, with the PG sink
      load                         in-process, one transaction
      release, finish, summarise   which closes the run if it was the last job
    on SIGTERM: drain, then requeue anything unfinished, then mark stopped
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg

from mb_ceramics_catalogue import __version__, scrapers
from mb_ceramics_catalogue.config.settings import CrawlParams, Settings
from mb_ceramics_catalogue.config.sources import SourcesFile
from mb_ceramics_catalogue.crawl import artifacts
from mb_ceramics_catalogue.crawl.progress import Progress
from mb_ceramics_catalogue.crawl.runner import barren as run_source_barren
from mb_ceramics_catalogue.crawl.runner import run_source
from mb_ceramics_catalogue.crawl.session import open_session
from mb_ceramics_catalogue.observability import logging as obs
from mb_ceramics_catalogue.observability import metrics, tracing
from mb_ceramics_catalogue.ops import events, leases, monitor, queue, runs, schedule
from mb_ceramics_catalogue.ops.sink import JobLogHandler, PostgresSink
from mb_ceramics_catalogue.scrapers.activity import CURRENT_JOB
from mb_ceramics_catalogue.scrapers.base import BrowserRenderer, BrowserUnavailable
from mb_ceramics_catalogue.scrapers.record import RecordBuilder
from mb_ceramics_catalogue.storage import postgres
from mb_ceramics_catalogue.storage.db import DictPool

LOGGER = obs.get_logger("catalogue.worker")

#: How often the worker reports that it is alive and renews its leases.
HEARTBEAT_SECONDS = 5.0

#: How long to wait after finding nothing to do. Long enough not to hammer the
#: database, short enough that "Run now" in the UI feels immediate.
IDLE_SECONDS = 2.0

#: How long a drain waits for the current source before giving up on it.
DRAIN_GRACE_SECONDS = 120.0

#: How often a worker attempts the leader duties. Every tick would mean eighty
#: advisory-lock attempts a second across a busy pool for work that is only
#: meaningful once a minute.
LEADER_SECONDS = 30.0

#: Retention runs far less often than the notification rules; deleting a month
#: of rows is not something to do every half minute.
PRUNE_SECONDS = 3600.0


@dataclass
class WorkerState:
    """Everything about this process that the database also knows."""

    id: UUID = field(default_factory=uuid4)
    hostname: str = field(default_factory=socket.gethostname)
    pid: int = field(default_factory=os.getpid)
    capabilities: list[str] = field(default_factory=list)
    status: str = "starting"
    desired_state: str = "running"
    #: Jobs this process is running right now, by job id. A worker may hold
    #: several: they are different sources on different hosts, and the thing
    #: that stops two of them hammering one shop is `catalogue.host_leases`,
    #: not the fact that a process happened to do one at a time.
    current_jobs: dict[UUID, queue.ClaimedJob] = field(default_factory=dict)
    stopping: bool = False

    @property
    def current_job(self) -> queue.ClaimedJob | None:
        """The job to name in single-valued places, e.g. `workers.current_job_id`."""
        return next(iter(self.current_jobs.values()), None)


class Worker:
    """One worker process."""

    def __init__(
        self,
        pool: DictPool,
        sources: SourcesFile,
        settings: Settings,
        *,
        capabilities: list[str] | None = None,
        once: bool = False,
    ) -> None:
        self.pool = pool
        self.sources = sources
        self.settings = settings
        self.state = WorkerState(capabilities=capabilities or [])
        self.once = once
        self._task: asyncio.Task[Any] | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        #: One cancel flag per running job: cancelling one source must not
        #: tear down the others this process is carrying.
        self._cancels: dict[UUID, asyncio.Event] = {}
        self._slots = asyncio.Semaphore(max(1, settings.job_slots))
        #: One camoufox for this process, shared by every job that renders and
        #: started on the first one that does. Per job it was sixteen across the
        #: fleet; see `BrowserRenderer`.
        self._browser: BrowserRenderer | None = None
        # Negative, so the first tick leads immediately rather than waiting out
        # the interval: a worker starting after downtime should notice a missed
        # schedule now, not in thirty seconds.
        self._last_lead = -LEADER_SECONDS
        self._last_prune = -PRUNE_SECONDS

    def describe(self) -> dict[str, Any]:
        """What `/health` reports. Read from a thread, so it touches no I/O."""
        return {
            "status": self.state.status,
            "worker_id": str(self.state.id),
            "desired_state": self.state.desired_state,
            "capabilities": self.state.capabilities,
            "current_source": getattr(self.state.current_job, "source_id", None),
            "version": __version__,
        }

    # -- lifecycle --------------------------------------------------------

    async def register(self) -> None:
        async with self.pool.connection() as connection:
            await connection.execute(
                """
                insert into catalogue.workers
                       (id, hostname, pid, version, capabilities, status)
                values (%(id)s, %(host)s, %(pid)s, %(version)s, %(caps)s, 'starting')
                on conflict (id) do update
                   set status = 'starting', last_heartbeat_at = now()
                """,
                {
                    "id": self.state.id,
                    "host": self.state.hostname,
                    "pid": self.state.pid,
                    "version": __version__,
                    "caps": self.state.capabilities,
                },
            )
            await events.emit(
                connection,
                events.Topic.WORKER,
                "worker.starting",
                worker_id=self.state.id,
                payload={
                    "hostname": self.state.hostname,
                    "pid": self.state.pid,
                    "version": __version__,
                    "capabilities": self.state.capabilities,
                },
            )
        obs.bind(worker_id=str(self.state.id))
        LOGGER.info(
            "worker.starting",
            hostname=self.state.hostname,
            pid=self.state.pid,
            capabilities=self.state.capabilities,
            version=__version__,
        )

    async def set_status(
        self, status: str, *, job_id: UUID | None = None, force: bool = False
    ) -> None:
        """Record a status change, and emit it as an edge.

        Status is an edge — `idle -> busy` is discrete and worth pushing to a
        browser. Liveness is a level and is carried by `last_heartbeat_at`,
        which the UI turns into an age locally. A worker that has silently died
        therefore goes stale on its own, without any event arriving — which is
        precisely the case where waiting for an event cannot work.
        """
        if (
            not force
            and self.state.status == status
            and job_id == getattr(self.state.current_job, "id", None)
        ):
            return
        self.state.status = status
        async with self.pool.connection() as connection:
            await connection.execute(
                "update catalogue.workers set status = %(status)s, current_job_id = %(job)s, "
                "last_heartbeat_at = now() where id = %(id)s",
                {"status": status, "job": job_id, "id": self.state.id},
            )
            await events.emit(
                connection,
                events.Topic.WORKER,
                "worker.changed",
                worker_id=self.state.id,
                job_id=job_id,
                payload={"status": status, "current_job_id": str(job_id) if job_id else None},
            )

    async def _beat(self) -> None:
        """Report liveness, renew leases, and read the control flags.

        On its own connection from the pool, in its own task. If this shared the
        job's connection it would stop while the job held it, and a healthy
        worker crawling a slow shop would look dead — after which its job would
        be reaped and run twice.
        """
        while not self.state.stopping:
            try:
                async with self.pool.connection() as connection:
                    row = await _one(
                        connection,
                        "update catalogue.workers set last_heartbeat_at = now() "
                        "where id = %(id)s returning desired_state",
                        {"id": self.state.id},
                    )
                    if row is not None:
                        await self._observe_desired_state(str(row["desired_state"]))

                    held = await queue.renew(connection, self.state.id)
                    await leases.renew(connection, self.state.id)

                    for job in held:
                        if job["cancel_requested"]:
                            LOGGER.info("job.cancel_requested", job_id=str(job["id"]))
                            self._cancel(job["id"])
                        elif job["pause_requested"]:
                            # Pause is implemented as a cancel that keeps the
                            # partial artifact and leaves the job resumable,
                            # rather than as a held-open in-memory session: a
                            # worker holding a browser and a half-read catalogue
                            # for an unbounded time is a much worse failure than
                            # restarting the source.
                            LOGGER.info("job.pause_requested", job_id=str(job["id"]))
                            self._cancel(job["id"])
            except psycopg.Error:
                # A heartbeat that cannot reach the database is exactly when the
                # worker must not fall over: the database may be restarting, and
                # the lease has minutes left on it.
                LOGGER.warning("worker.heartbeat_failed", exc_info=True)

            await asyncio.sleep(HEARTBEAT_SECONDS)

    async def _observe_desired_state(self, desired: str) -> None:
        if desired == self.state.desired_state:
            return
        self.state.desired_state = desired
        LOGGER.info("worker.desired_state", desired=desired)
        if desired == "stopping":
            # Cancels every source in flight through the same safe
            # partial-artifact path a per-job cancel uses, then exits.
            self._cancel_all()
            self.state.stopping = True
        elif desired == "draining":
            self.state.stopping = True
        elif desired == "paused":
            await self.set_status("paused")
        elif desired == "running" and self.state.status == "paused":
            await self.set_status("idle")

    # -- the loop ---------------------------------------------------------

    async def run(self) -> int:
        await self.register()
        self._heartbeat = asyncio.create_task(self._beat(), name="worker-heartbeat")
        await self.set_status("idle")
        LOGGER.info("worker.ready", capabilities=self.state.capabilities)

        completed = 0
        running: set[asyncio.Task[bool]] = set()
        try:
            while not self.state.stopping:
                if self.state.desired_state in ("paused",):
                    await asyncio.sleep(IDLE_SECONDS)
                    continue

                # Fill the free slots before waiting on any of them. Sources are
                # independent and mostly waiting on someone else's network, so a
                # worker that runs them one at a time is idle for most of a run;
                # `catalogue.host_leases` is what keeps two jobs off one shop,
                # and it works within a process exactly as it does between two.
                while len(running) < self.settings.job_slots and not self.state.stopping:
                    claimed = await self.tick(spawn=running)
                    if not claimed:
                        break

                if not running:
                    if self.once:
                        break
                    await asyncio.sleep(IDLE_SECONDS)
                    continue

                done, running = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                worked = False
                for task in done:
                    try:
                        worked = task.result() or worked
                    except Exception:
                        # execute() already recorded the job's own outcome; this
                        # is the task machinery failing, which is ours not the
                        # source's, and must not stop the other slots.
                        LOGGER.exception("worker.job_task_failed")
                if worked:
                    completed += 1
                    if self.once:
                        break
                    if self.settings.max_jobs and completed >= self.settings.max_jobs:
                        # A clean exit between jobs, not a crash during one. The
                        # restart policy brings up a fresh process with a fresh
                        # browser; nothing is requeued because nothing is in
                        # flight at this point.
                        LOGGER.info("worker.recycling", completed=completed,
                                    max_jobs=self.settings.max_jobs)
                        self.state.desired_state = "draining"
                        break
        finally:
            # Let whatever is still in flight finish its own cancellation path
            # before the connection pool goes away underneath it.
            for task in running:
                task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
            await self.shutdown()
        return completed

    async def lead(self) -> None:
        """Do the things exactly one worker should do, if this one may.

        Scheduling, the notification rules and retention all need a single
        actor. Rather than a scheduler container — one more thing to deploy and
        a single point of failure — every worker tries a transaction-scoped
        advisory lock each tick and whichever gets it does the work. A
        transaction-scoped lock cannot leak or accumulate on a pooled session,
        which a session-scoped one can.
        """
        now = time.monotonic()
        if now - self._last_lead < LEADER_SECONDS:
            return
        self._last_lead = now
        try:
            async with self.pool.connection() as connection, connection.transaction():
                if not await schedule.try_become_leader(connection):
                    return
                await schedule.fire_due(connection, self.sources)
                await monitor.check_all(connection)
                if now - self._last_prune > PRUNE_SECONDS:
                    self._last_prune = now
                    await monitor.prune(connection)
        except psycopg.Error:
            # Leading is maintenance. A worker that cannot do it should keep
            # crawling; another worker will hold the lock next tick.
            LOGGER.warning("worker.lead_failed", exc_info=True)

    async def tick(self, spawn: set[asyncio.Task[bool]] | None = None) -> bool:
        """One pass: lead, recover, claim, run.

        With `spawn`, the claimed job is started as a task in that set and this
        returns whether one was claimed; the caller decides when to wait. Without
        it the job is run to completion inline, which is what `--once` and the
        tests want.
        """
        await self.lead()
        async with self.pool.connection() as connection:
            await queue.reap_expired(connection)
            job = await queue.claim(connection, self.state.id, self.state.capabilities)

        if job is None:
            return False

        async def run_one() -> bool:
            # Set inside the task, so every line logged under this job — and
            # under any task it starts — carries the job it belongs to and no
            # other job's log sink accepts it. See `sink.JobLogHandler.emit`.
            CURRENT_JOB.set(str(job.id))
            with obs.bound(job_id=str(job.id), run_id=str(job.run_id), source=job.source_id):
                return await self.execute(job)

        if spawn is None:
            return await run_one()
        spawn.add(asyncio.create_task(run_one(), name=f"job:{job.source_id}"))
        return True

    async def execute(self, job: queue.ClaimedJob) -> bool:
        """Take a claimed job all the way to a terminal state."""
        self.state.current_jobs[job.id] = job
        self._cancels[job.id] = asyncio.Event()

        # Both keys this job has to be polite under: its own shop, and — for a
        # storefront family that answers from one provider's edge — that edge.
        # Taken in a fixed order, shop first, so two workers claiming the same
        # pair cannot deadlock against each other.
        # Asked of the source only if this worker still has one for it. A job
        # queued before a source was removed from sources.json is a job that
        # fails, further down and where a failure is recorded — not one that
        # raises out here, before the try, holding a lease nobody releases.
        keys = [job.host]
        known = self.sources.get(job.source_id)
        if known and (edge := scrapers.shared_edge(known.scraper)):
            keys.append(edge)

        async with self.pool.connection() as connection:
            for key in keys:
                if await leases.acquire(connection, key, job.id, self.state.id) is None:
                    # Another worker is crawling this shop, or another shop on
                    # the same edge. Not an attempt: being polite must not spend
                    # a source's retry budget. Anything already taken for this
                    # job goes back, or the second key's contention would leak
                    # the first key's slot until its lease expired.
                    await leases.release_all(connection, job.id)
                    await queue.release(connection, job, self.state.id, reason="host busy")
                    self._forget(job)
                    return False

        started = time.monotonic()
        with tracing.span(
            "job",
            **{
                "catalogue.source": job.source_id,
                "catalogue.run_id": str(job.run_id),
                "catalogue.attempt": job.attempt,
            },
        ):
            trace_id = tracing.trace_id()
            async with self.pool.connection() as connection:
                if not await queue.start(connection, job, self.state.id, trace_id=trace_id):
                    await leases.release_all(connection, job.id)
                    self._forget(job)
                    return False
                # Whichever worker gets there first moves the run out of
                # `queued`. It is conditional on the current status, so the
                # other seventy-nine jobs starting are no-ops rather than a
                # stream of redundant `run.started` edges.
                await runs.start_run(connection, job.run_id)
            await self.set_status("busy", job_id=job.id)

            try:
                await self._crawl_and_load(job)
            except BrowserUnavailable as error:
                if not await self._requeue_for_browser(job, error):
                    # Already required a browser and still could not get one, so
                    # rerouting it again would only find the same wall. This is
                    # the image being wrong, not the source, but a failed job an
                    # operator can see beats one circling the queue unnoticed.
                    LOGGER.exception("job.failed", source=job.source_id)
                    await self._finish(job, "failed", error=str(error)[:2000])
            except Exception as error:
                LOGGER.exception("job.failed", source=job.source_id)
                await self._finish(job, "failed", error=str(error)[:2000])
            finally:
                async with self.pool.connection() as connection:
                    await leases.release_all(connection, job.id)
                metrics.job_duration(job.source_id, time.monotonic() - started)
                self._forget(job)
                # Another slot may still be crawling. A completed job used to
                # unconditionally publish `idle`, and the in-memory shortcut in
                # set_status could also leave current_job_id pointing at the job
                # that just finished. Re-project the aggregate process state.
                remaining = self.state.current_job
                await self.set_status(
                    "busy" if remaining else "idle",
                    job_id=remaining.id if remaining else None,
                    force=True,
                )
        return True

    def _renderer(self, params: CrawlParams) -> BrowserRenderer | None:
        """This process's one browser, started on the first job that needs it.

        Returning None for a job that has rendering switched off lets the
        session build its own disabled renderer, so `--browser never` still
        means "this crawl does not render" rather than "this crawl may use the
        shared one".
        """
        if params.browser == "never":
            return None
        if self._browser is None:
            self._browser = BrowserRenderer(True, pages=self.settings.browser_pages)
        return self._browser

    async def _close_browser(self) -> None:
        """Shut the shared browser down. Idempotent, and never fatal."""
        browser, self._browser = self._browser, None
        if browser is None:
            return
        try:
            await browser.close()
        except Exception:
            # A stuck browser must not block a drain: the jobs are already back
            # on the queue and the process is about to exit regardless.
            LOGGER.warning("worker.browser_close_failed", exc_info=True)

    async def _crawl_and_load(self, job: queue.ClaimedJob) -> None:
        """Collect one source, write its artifact, load it, and finish the job."""
        params = CrawlParams.from_job(job.params)
        config = self.sources[job.source_id]
        output = artifacts.job_directory(self.settings.dumps_dir, str(job.run_id), str(job.id))

        log_handler = JobLogHandler(job.id)
        obs.attach(log_handler)

        cancelled = False
        try:
            async with self.pool.connection() as connection:
                sink = PostgresSink(connection, job.run_id, {job.source_id: job.id})
                with RecordBuilder(self.sources.as_scraper_configs()):
                    async with (
                        open_session(
                            params, self.settings.cache_dir, browser=self._renderer(params)
                        ) as session,
                        Progress(1, [sink]) as progress,
                    ):
                        task = asyncio.create_task(
                            run_source(job.source_id, config, session, params, progress, None),
                            name=f"job:{job.source_id}",
                        )
                        watcher = asyncio.create_task(self._watch_for_cancel(job.id, task))
                        try:
                            outcome = await task
                        except asyncio.CancelledError:
                            cancelled = True
                            outcome = None
                        finally:
                            watcher.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await watcher

                        if cancelled:
                            # Keep what was collected. `plan_load` refuses to
                            # retire against a partial, so this is safe to load
                            # and worth loading: an hour of a large storefront
                            # is not nothing.
                            records = list(
                                getattr(progress.results.get(job.source_id), "records", [])
                            )
                            artifact = artifacts.write_partial(output, job.source_id, records)
                            await self._finish(
                                job, "cancelled", artifact=artifact,
                                summary={"records": len(records), "interrupted": True},
                            )
                            return

                assert outcome is not None
                artifact = artifacts.write_source(
                    output, job.source_id, outcome.records, params.allow_empty
                )
                outcome.summary["write_status"] = artifact.status
                await events.log(
                    connection, job.id,
                    f"wrote {artifact.size} bytes to {artifact.path.name}",
                    event="job.artifact", data={"sha256": artifact.sha256},
                )
                await log_handler.flush_to(connection)

            loaded = await self._load(job, outcome, whole=artifact.status == "replaced")
            outcome.summary["loaded"] = loaded.records
            outcome.summary["retired"] = loaded.retired
            if loaded.rejected:
                # On the summary rather than only in the log: a source quietly
                # dropping rows at the database is exactly the kind of thing
                # that goes unnoticed while every job stays green.
                outcome.summary["rejected"] = loaded.rejected
                outcome.summary["rejects"] = loaded.rejects

            # Two ways to have collected nothing. The first is the loud one:
            # something refused us and said so. The second is silent — every
            # request answered, nothing recognised — and used to report success.
            error = None
            if outcome.summary["error_count"] and not outcome.summary["records"]:
                error = _first_error(outcome.summary)
            elif nothing := run_source_barren(outcome.summary):
                error = nothing
            await self._finish(
                job,
                "failed" if error else "succeeded",
                summary=outcome.summary,
                artifact=artifact,
                error=error,
            )
        finally:
            obs.detach(log_handler)
            async with self.pool.connection() as connection:
                await log_handler.flush_to(connection)

    async def _requeue_for_browser(
        self, job: queue.ClaimedJob, error: BrowserUnavailable
    ) -> bool:
        """Send a job that turned out to need a browser to a worker that has one.

        `BROWSER_SOURCES` names the sources whose scrapers always render, but a
        plain source escalates too: `pagecrawl` retries a page through the
        browser when it parses to nothing, which depends on what the shop
        served today. So the requirement cannot be fully known when the job is
        enqueued, and the honest place to discover it is here.

        Nothing collected so far is kept. The browser worker starts the source
        again from the beginning, which the response cache makes cheap, and a
        partial artifact from an aborted attempt would otherwise have to be
        reconciled with the complete one that replaces it.
        """
        async with self.pool.connection() as connection:
            return await queue.require_capability(
                connection, job, self.state.id, "browser", reason=str(error)[:200]
            )

    async def _watch_for_cancel(self, job_id: UUID, task: asyncio.Task[Any]) -> None:
        """Cancel one running source once the heartbeat sees its flag."""
        await self._cancels[job_id].wait()
        if not task.done():
            task.cancel()

    def _cancel(self, job_id: Any) -> None:
        """Raise the cancel flag for one job, if this process is running it."""
        event = self._cancels.get(UUID(str(job_id)))
        if event is not None:
            event.set()

    def _cancel_all(self) -> None:
        """Stop every source this process is carrying, e.g. on a second signal."""
        for event in self._cancels.values():
            event.set()

    def _forget(self, job: queue.ClaimedJob) -> None:
        self.state.current_jobs.pop(job.id, None)
        self._cancels.pop(job.id, None)

    async def _load(self, job: queue.ClaimedJob, outcome: Any, *, whole: bool) -> postgres.SourceReport:
        """Load this source's records, in a thread so the loop keeps beating.

        `storage.postgres` is synchronous — one transaction, a COPY and a
        `load_record` per row — and a 4,000-record source takes seconds. Running
        it inline would stall the event loop, and with it the heartbeat.
        """
        dsn = self.settings.dsn

        def load() -> postgres.SourceReport:
            from psycopg.rows import dict_row

            with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as connection:
                postgres.ensure_staging(connection)
                return postgres.load_source(
                    connection,
                    job.source_id,
                    outcome.records,
                    whole=whole,
                    run_id=None,
                )

        return await asyncio.to_thread(load)

    async def _finish(
        self,
        job: queue.ClaimedJob,
        state: str,
        *,
        summary: dict[str, Any] | None = None,
        error: str | None = None,
        artifact: Any = None,
    ) -> None:
        async with self.pool.connection() as connection:
            await runs.finish_job(
                connection, job.id, state=state, summary=summary, error=error, artifact=artifact
            )
            if state == "failed" and job.attempt >= job.max_attempts:
                await events.notify(
                    connection,
                    "job.failed",
                    f"{job.source_id} failed {job.attempt} times",
                    body=error,
                    run_id=job.run_id,
                    job_id=job.id,
                    source_id=job.source_id,
                )
            elif state == "succeeded":
                # The condition has ended, so the warning should stop being
                # shown. An alert that never clears is one nobody reads.
                await events.resolve(connection, f"job.failed:{job.source_id}:",
                                     source_id=job.source_id)

    # -- shutdown ---------------------------------------------------------

    async def shutdown(self) -> None:
        """Stop cleanly, giving back anything not finished."""
        self.state.stopping = True
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat

        try:
            async with self.pool.connection() as connection:
                for job in list(self.state.current_jobs.values()):
                    # Requeued rather than failed: the worker is going away, and
                    # that is not the source's fault, so no attempt is spent.
                    await leases.release_all(connection, job.id)
                    await queue.release(
                        connection, job, self.state.id, delay=0, reason="worker stopping"
                    )
                await connection.execute(
                    "update catalogue.workers set status = 'stopped', current_job_id = null, "
                    "last_heartbeat_at = now() where id = %(id)s",
                    {"id": self.state.id},
                )
                await events.emit(
                    connection, events.Topic.WORKER, "worker.stopped",
                    worker_id=self.state.id, payload={"reason": self.state.desired_state},
                )
        except psycopg.Error:
            # Nothing left to do about it. The lease will expire and another
            # worker will pick the job up, which is the whole reason leases
            # expire rather than being released.
            LOGGER.warning("worker.shutdown_incomplete", exc_info=True)

        # After the jobs are back on the queue, and outside the database's
        # error path: a browser this process leaves running outlives the
        # container's stop grace period as an orphan.
        await self._close_browser()

        obs.unbind("worker_id")
        LOGGER.info("worker.stopping", reason=self.state.desired_state)

    def install_signal_handlers(self) -> None:
        """SIGTERM behaves like drain, bounded by the shutdown grace period.

        Containers and systemd send SIGTERM. Without this the process is killed
        mid-`write_source`, and the job stays `running` until its lease expires
        rather than going straight back on the queue.
        """
        loop = asyncio.get_running_loop()
        for name in ("SIGTERM", "SIGINT"):
            signum = getattr(signal, name, None)
            if signum is None:  # pragma: no cover - Windows
                continue
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(signum, self._on_signal, name)

    def _on_signal(self, name: str) -> None:
        if self.state.stopping:
            # A second signal means "now". Cancel the source through the safe
            # partial path rather than waiting out the grace period.
            LOGGER.warning("worker.stop_forced", signal=name)
            self._cancel_all()
            return
        LOGGER.info("worker.stopping", signal=name, grace=DRAIN_GRACE_SECONDS)
        self.state.stopping = True
        self.state.desired_state = "draining"
        loop = asyncio.get_running_loop()
        loop.call_later(DRAIN_GRACE_SECONDS, self._cancel_all)


def _first_error(summary: dict[str, Any]) -> str | None:
    errors = summary.get("errors") or []
    return str(errors[0].get("error"))[:2000] if errors else None


async def _one(
    connection: psycopg.AsyncConnection[dict[str, Any]], sql: str, params: Any = None
) -> dict[str, Any] | None:
    async with connection.cursor() as cursor:
        await cursor.execute(sql, params)
        return await cursor.fetchone()


def dumps_root(settings: Settings) -> Path:
    return settings.dumps_dir
