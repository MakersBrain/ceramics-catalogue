"""The registry `catalogue-ops.openapi.json` is generated from.

A separate document from the catalogue's, deliberately (§10.1). Different
audience, different auth, different guarantees — merging them would put a
run-cancel endpoint in the document a tenant reads.

`GET /v1/events` is described as a `text/event-stream` response. OpenAPI 3.1 can
name the media type but not the schema of each named SSE event, so every payload
is defined in `components/schemas` and the operation description maps event
names onto them. The tooling does not enforce that mapping — but the schemas are
generated from the same models the service serialises with, so the payloads
cannot drift even though the association is prose.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from ateliera_catalogue.config.settings import CrawlParams
from ateliera_catalogue.contracts import Operation, Parameter, Registry
from pydantic import BaseModel, Field

VERSION = "0.2.0"

DESCRIPTION = """
Operator control for the ceramics catalogue: start and watch runs, control
workers, enable and pause sources, and read the durable notification feed.

Internal. Every `/v1` route including the stream requires a bearer token;
`/health` and `/metrics` are the only exemptions. The service is not published
on the host, which is defence in depth rather than the authentication boundary.

### The live stream

`GET /v1/events` is one multiplexed `text/event-stream`, filtered by `topics`.
One endpoint rather than one per concern, because HTTP/1.1 caps a browser at
roughly six connections per origin.

Each message uses the SSE `event:` field, so a client writes
`stream.addEventListener('worker.changed', …)` rather than discriminating a
union. **Which messages carry an `id:` is the contract**:

| `event:` | Schema | Numbered | Replayed |
|---|---|---|---|
| `bootstrap` | `Bootstrap` | no | no |
| `worker.roster` | `WorkerRoster` | no | no |
| `worker.changed` | `WorkerChanged` | yes | yes |
| `run.started` / `run.complete` / `run.degraded` / `run.failed` | `RunEvent` | yes | yes |
| `job.leased` / `job.started` / `job.succeeded` / `job.failed` / `job.cancelled` | `JobStateChanged` | yes | yes |
| `job.progress` | `JobProgress` | no | no |
| `notification.raised` / `notification.resolved` | `NotificationEvent` | yes | yes |
| `resync` | `Resync` | no | no |

Numbered events are edges: discrete, ordered by one bigint sequence, and
replayable from `Last-Event-ID`. Unnumbered ones are levels — current values
that are meaningful only as the latest reading. A client that missed forty
progress readings does not want them, it wants the current one.

On `resync`, refetch state over the JSON endpoints; the gap was too large to
replay.
""".strip()


# ---------------------------------------------------------------------------
# Request and response models
# ---------------------------------------------------------------------------


class CreateRunRequest(BaseModel):
    sources: str = Field(
        default="all", description="A source id, a comma-separated list, or 'all'."
    )
    kind: Literal["manual", "scheduled", "retry", "backfill"] = "manual"
    requested_by: str | None = None
    params: CrawlParams = Field(
        default_factory=CrawlParams,
        description=(
            "Validated against the same model the CLI and the scheduler use, so a run "
            "created here cannot mean something a run created there would not."
        ),
    )


class CreateRunResponse(BaseModel):
    run_id: str
    jobs: int
    sources: list[str]


class RunSummary(BaseModel):
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    skipped: int = 0
    records: int = 0
    requests: int = 0


class Run(BaseModel):
    id: str
    kind: str
    status: Literal["queued", "running", "complete", "degraded", "failed", "cancelled"]
    schedule_id: str | None = None
    requested_by: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    summary: RunSummary | None = None
    jobs: int = 0
    succeeded: int = 0
    failed: int = 0
    active: int = 0


class RunList(BaseModel):
    runs: list[Run]
    next_cursor: str | None = None


class InFlight(BaseModel):
    seconds: float
    url: str


class JobSummary(BaseModel):
    """The per-source summary `run_source` produces, stored verbatim on the job.

    Typed rather than a free `object`: the job detail page reads
    `field_coverage` and `errors` off it, and a `Record<string, unknown>` is
    exactly the drift this whole exercise removes.
    """

    source: str
    label: str | None = None
    scraper: str
    extraction_method: str | None = None
    records: int = 0
    discovered: int = 0
    requests: int = 0
    rendered_pages: int = 0
    truncated: bool = False
    robots_ignored: bool = False
    error_count: int = 0
    errors: list[dict[str, str]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    field_coverage: dict[str, int] = Field(
        default_factory=dict,
        description="Rows carrying each field, so a thin scraper is visible.",
    )
    write_status: str | None = None
    interrupted: bool | None = None
    loaded: int | None = None
    retired: int | None = None


class Job(BaseModel):
    id: str
    run_id: str
    source_id: str
    host: str
    state: Literal[
        "queued", "leased", "running", "paused", "succeeded", "failed", "cancelled", "skipped"
    ]
    attempt: int
    max_attempts: int
    priority: int
    requires: list[str] = Field(default_factory=list)
    scheduled_for: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    trace_id: str | None = Field(
        default=None, description="32 lower-case hex characters when tracing is active."
    )
    artifact_path: str | None = None
    artifact_sha256: str | None = None
    artifact_size: int | None = None
    cancel_requested: bool = False
    pause_requested: bool = False
    summary: JobSummary | None = None
    phase: str | None = None
    records: int | None = None
    requests: int | None = None
    rendered_pages: int | None = None
    error_count: int | None = None
    discovered: int | None = None
    truncated: bool | None = None
    in_flight: list[InFlight] | None = None
    previous_records: int | None = Field(
        default=None,
        description="The previous successful run's record count, so a progress bar has a scale.",
    )


class RunDetail(BaseModel):
    run: Run
    jobs: list[Job]


class JobDetail(BaseModel):
    job: Job


class Accepted(BaseModel):
    """A control was applied. 202 rather than 200: the worker acts on it later."""

    job_id: str | None = None
    worker_id: str | None = None
    action: str | None = None
    desired_state: str | None = None
    cancelled: int | None = None


class LogLine(BaseModel):
    id: int
    at: datetime
    level: Literal["debug", "info", "warning", "error"]
    event: str | None = None
    message: str
    data: dict[str, Any] | None = None


class LogPage(BaseModel):
    lines: list[LogLine]
    next_after: int | None = None


class Worker(BaseModel):
    worker_id: str
    hostname: str
    pid: int
    version: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    status: Literal["starting", "idle", "busy", "paused", "draining", "stopped"]
    desired_state: Literal["running", "paused", "draining", "stopping"]
    started_at: datetime
    last_heartbeat_at: datetime = Field(
        description=(
            "Derive the age from this locally. A worker that has silently died emits "
            "no event — nothing fires when a process stops existing — so staleness has "
            "to be computed from a clock rather than waited for."
        )
    )
    current_job_id: str | None = None
    current_source: str | None = None
    heartbeat_age_seconds: float | None = None


class WorkerList(BaseModel):
    workers: list[Worker]


class Source(BaseModel):
    source_id: str
    label: str
    url: str
    scraper: str
    country: str | None = None
    enabled: bool = True
    paused: bool = False
    schedule_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    last_success_at: datetime | None = None
    last_records: int | None = None
    previous_records: int | None = None
    delta: int | None = None
    staleness_seconds: float | None = None
    runs_7d: int = 0
    failures_7d: int = 0


class SourceList(BaseModel):
    sources: list[Source]


class SourceSettings(BaseModel):
    enabled: bool = True
    paused: bool = Field(
        default=False,
        description=(
            "Pausing also pauses this source's jobs that are in flight. Resuming does "
            "not automatically resume individually paused jobs: a broad administrative "
            "toggle must not silently restart work somebody stopped on purpose."
        ),
    )
    schedule_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    updated_by: str | None = None


class SourceUpdated(BaseModel):
    source: dict[str, Any]


class Notification(BaseModel):
    id: int
    at: datetime
    severity: Literal["info", "warning", "critical"]
    kind: str
    title: str
    body: str | None = None
    run_id: str | None = None
    job_id: str | None = None
    source_id: str | None = None
    worker_id: str | None = None
    dedup_key: str
    resolved_at: datetime | None = None
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None


class NotificationList(BaseModel):
    notifications: list[Notification]


class Acknowledgement(BaseModel):
    id: int
    acknowledged: bool


class AcknowledgeRequest(BaseModel):
    by: str | None = None


class Schedule(BaseModel):
    id: str
    enabled: bool = True
    cron: str = Field(description="Five-field cron, evaluated in `timezone`.")
    timezone: str = "Europe/Paris"
    source_filter: dict[str, Any] = Field(default_factory=lambda: {"all": True})
    params: dict[str, Any] = Field(default_factory=dict)
    last_fired_at: datetime | None = None
    next_fire_at: datetime | None = None


class ScheduleList(BaseModel):
    schedules: list[Schedule]


class ScheduleUpdated(BaseModel):
    schedule: Schedule


class Health(BaseModel):
    status: Literal["ok", "unavailable"]


# -- stream payloads --------------------------------------------------------


class JobProgress(BaseModel):
    """A level. Unnumbered and never replayed; the counters are cumulative."""

    job_id: str
    run_id: str
    source: str
    phase: str | None = None
    records: int = 0
    requests: int = 0
    rendered_pages: int = 0
    errors: int = 0
    discovered: int = 0
    truncated: bool = False
    in_flight: list[InFlight] = Field(default_factory=list)
    at: datetime


class WorkerRoster(BaseModel):
    """A level, pushed every five seconds."""

    workers: list[Worker]


class WorkerChanged(BaseModel):
    """An edge: `idle -> busy`, a drain, a stop."""

    id: int
    at: datetime
    type: str
    worker_id: str
    status: str | None = None
    current_job_id: str | None = None
    desired_state: str | None = None


class RunEvent(BaseModel):
    id: int
    at: datetime
    type: str
    run_id: str
    succeeded: int | None = None
    failed: int | None = None
    records: int | None = None


class JobStateChanged(BaseModel):
    id: int
    at: datetime
    type: str
    run_id: str
    job_id: str
    source: str
    state: str | None = None
    attempt: int | None = None
    records: int | None = None
    error: str | None = None


class NotificationEvent(BaseModel):
    id: int
    at: datetime
    type: str
    severity: str | None = None
    kind: str | None = None
    title: str | None = None
    source: str | None = None


class Bootstrap(BaseModel):
    """The stream's first frame, so a client renders without a second request."""

    workers: list[Worker]
    active_runs: list[dict[str, Any]]
    notifications: list[Notification]
    queue: dict[str, int]
    watermark: int
    jobs: list[Job] | None = None


class Resync(BaseModel):
    """The gap was too large to replay. Refetch over the JSON endpoints."""

    reason: str


def registry() -> Registry:
    api = Registry(
        title="Ceramics catalogue operations",
        version=VERSION,
        description=DESCRIPTION,
        servers=[{"url": "/", "description": "the control service"}],
        security=True,
    )

    api.add(Operation("get", "/health", "health", "Liveness", response=Health, errors=(503,), tags=("service",)))
    api.add(
        Operation(
            "get", "/metrics", "metrics", "Prometheus metrics",
            media_type="text/plain", errors=(), tags=("service",),
        )
    )

    api.add(
        Operation(
            "post", "/v1/runs", "createRun", "Start a run",
            description="Creates the run and one job per selected source. 202: the workers pick them up.",
            request=CreateRunRequest, response=CreateRunResponse, status=202,
            errors=(401, 409, 422), tags=("runs",),
        )
    )
    api.add(
        Operation(
            "get", "/v1/runs", "listRuns", "Run history",
            parameters=(
                Parameter("limit", schema={"type": "integer", "maximum": 200, "default": 25}),
                Parameter("cursor", description="A previous page's `next_cursor`."),
            ),
            response=RunList, errors=(401,), tags=("runs",),
        )
    )
    api.add(
        Operation(
            "get", "/v1/runs/{id}", "getRun", "One run with its jobs and their progress",
            parameters=(Parameter("id", location="path"),),
            response=RunDetail, errors=(400, 401, 404), tags=("runs",),
        )
    )
    api.add(
        Operation(
            "post", "/v1/runs/{id}/cancel", "cancelRun", "Cancel every unfinished job in a run",
            parameters=(Parameter("id", location="path"),),
            response=Accepted, status=202, errors=(400, 401), tags=("runs",),
        )
    )

    api.add(
        Operation(
            "get", "/v1/jobs/{id}", "getJob", "One job",
            parameters=(Parameter("id", location="path"),),
            response=JobDetail, errors=(400, 401, 404), tags=("jobs",),
        )
    )
    api.add(
        Operation(
            "get", "/v1/jobs/{id}/logs", "getJobLogs", "A job's log, cursor-paged",
            parameters=(
                Parameter("id", location="path"),
                Parameter("after", description="Return lines after this id.",
                          schema={"type": "integer", "default": 0}),
                Parameter("level", description="debug, info, warning or error."),
                Parameter("q", description="Substring match on the message."),
                Parameter("limit", schema={"type": "integer", "maximum": 2000, "default": 500}),
            ),
            response=LogPage, errors=(400, 401), tags=("jobs",),
        )
    )
    api.add(
        Operation(
            "post", "/v1/jobs/{id}/{action}", "controlJob",
            "pause, resume, cancel or retry one source",
            description=(
                "Four controls rather than one, because they have different safety "
                "properties. A **pause** keeps the job resumable and consumes no "
                "attempt. A **cancel** is terminal and keeps whatever was collected as a "
                "partial artifact, which the loader will add from but never retire "
                "against. A **retry** is a new attempt the operator asked for, so the "
                "attempt budget is reset. 409 means the job is not in a state where the "
                "action means anything — pressing the same button twice is a no-op."
            ),
            parameters=(
                Parameter("id", location="path"),
                Parameter("action", location="path",
                          schema={"type": "string", "enum": ["pause", "resume", "cancel", "retry"]}),
            ),
            response=Accepted, status=202, errors=(400, 401, 404, 409), tags=("jobs",),
        )
    )

    api.add(
        Operation(
            "get", "/v1/workers", "listWorkers", "The worker roster with heartbeat ages",
            response=WorkerList, errors=(401,), tags=("workers",),
        )
    )
    api.add(
        Operation(
            "post", "/v1/workers/{id}/{action}", "controlWorker",
            "pause, resume, drain or stop a worker, or hide a lost registration",
            description=(
                "Controls the registered process, not the deployment's replica count: a "
                "restart policy may create a new worker afterwards, so persistently "
                "removing capacity is a scale operation this API does not pretend to "
                "guarantee. Hide is different: it only removes a registration from the "
                "roster after its heartbeat is already stale; the audit row is retained."
            ),
            parameters=(
                Parameter("id", location="path"),
                Parameter("action", location="path",
                          schema={"type": "string", "enum": ["pause", "resume", "drain", "stop", "hide"]}),
            ),
            response=Accepted, status=202, errors=(400, 401, 404, 409), tags=("workers",),
        )
    )

    api.add(
        Operation(
            "get", "/v1/sources", "listSources",
            "Every configured source, joined to what actually happened to it",
            response=SourceList, errors=(401,), tags=("sources",),
        )
    )
    api.add(
        Operation(
            "put", "/v1/sources/{id}", "updateSource",
            "Enable, pause, or override a source's schedule and parameters",
            parameters=(Parameter("id", location="path"),),
            request=SourceSettings, response=SourceUpdated,
            errors=(401, 404, 422), tags=("sources",),
        )
    )

    api.add(
        Operation(
            "get", "/v1/schedules", "listSchedules", "Schedules",
            response=ScheduleList, errors=(401,), tags=("schedules",),
        )
    )
    api.add(
        Operation(
            "put", "/v1/schedules/{id}", "updateSchedule", "Create or edit a schedule",
            parameters=(Parameter("id", location="path"),),
            request=Schedule, response=ScheduleUpdated, errors=(401, 422), tags=("schedules",),
        )
    )

    api.add(
        Operation(
            "get", "/v1/notifications", "listNotifications", "The durable notification feed",
            parameters=(
                Parameter("unacknowledged", schema={"type": "boolean"},
                          description="Only conditions that are still open."),
                Parameter("severity", schema={"type": "string", "enum": ["info", "warning", "critical"]}),
                Parameter("limit", schema={"type": "integer", "maximum": 500, "default": 100}),
            ),
            response=NotificationList, errors=(401,), tags=("notifications",),
        )
    )
    api.add(
        Operation(
            "post", "/v1/notifications/{id}/ack", "acknowledgeNotification", "Acknowledge one",
            parameters=(Parameter("id", location="path", schema={"type": "integer"}),),
            request=AcknowledgeRequest, response=Acknowledgement,
            errors=(400, 401, 409), tags=("notifications",),
        )
    )

    api.add(
        Operation(
            "get", "/v1/events", "stream", "The live stream",
            description=(
                "Server-sent events. See the table in the document description for which "
                "`event:` names carry which schema, and which are numbered.\n\n"
                "Reconnect with `Last-Event-ID` to resume exactly; only numbered events "
                "are replayed."
            ),
            parameters=(
                Parameter(
                    "topics",
                    description=(
                        "Comma-separated: workers, runs, jobs, progress, notifications, "
                        "schedules, sources. Omitting this subscribes to everything "
                        "except `progress`, which is the expensive one and should be "
                        "asked for deliberately."
                    ),
                ),
                Parameter("run_id", description="Narrow `jobs` and `progress` to one run."),
                Parameter("Last-Event-ID", location="header",
                          description="Resume after this event id."),
            ),
            media_type="text/event-stream", errors=(401,), tags=("stream",),
        )
    )

    # The SSE payloads. Published as schemas without a path of their own: the
    # event table in the document description is what maps `event:` names onto
    # them, and inventing an endpoint per payload would put routes in the
    # document that do not exist.
    api.declare(
        Bootstrap, WorkerRoster, WorkerChanged, RunEvent, JobStateChanged,
        JobProgress, NotificationEvent, Resync,
    )

    return api
