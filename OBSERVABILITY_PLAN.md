# Observability plan: useful signals with bounded cost

## What this is

An audit of what the four services emit today and an ordered plan for making the
system operable. The goal is not to collect everything. Every metric, log field,
dashboard panel, and alert below must answer a concrete operator question.

The existing foundation stays:

* `observability/logging.py` provides structlog over the stdlib root logger,
  contextvar binding, secret scrubbing, and safe per-job log attachment.
* `observability/metrics.py` renders Prometheus-compatible metrics.
* `observability/tracing.py` creates spans and becomes active when an exporter is
  configured.

The immediate gaps are simpler than a new observability stack: job logs are not
live, structured context is dropped before persistence, two services lack
request logs, several defined metrics have no production call site, and the
query-backed gauges can retain stale series. Fix those before adding more
telemetry.

### Rules for new telemetry

1. A metric is accepted only with an operator question, a bounded label set, and
   either a dashboard query or an alert.
2. IDs, concrete URLs, exception text, operator identity, request IDs, trace IDs,
   and job/run IDs never become metric labels. They belong in logs.
3. `source`, normalized `host`, route template, state, and outcome are acceptable
   labels because their value sets are controlled and small.
4. Query-backed gauges represent the current database snapshot. Series absent
   from the new snapshot must be removed or explicitly set to zero.
5. Routine `/health` and `/metrics` requests are not access-logged. Long-lived
   SSE requests produce one completion/failure log, not periodic noise.
6. Retention and disk use are bounded before a backend is enabled.

---

## Phase 1 — Make existing job logs useful

These changes are local and make data already being produced available to the
operator.

### 1.1 Put the active trace id on log lines when one exists

**Today.** `observability/tracing.py` says the trace id is stamped onto jobs and
included in every log line. The job stamp exists, but the logging processor
chain does not read the active span.

**Change.** Add a processor to the shared chain:

```python
def _add_trace_context(_: Any, __: str, event: EventDict) -> EventDict:
    from . import tracing

    if identifier := tracing.trace_id():
        event.setdefault("trace_id", identifier)
    return event
```

Keep the import lazy because tracing is optional. Use `setdefault` so an
explicit field wins. This is cheap correlation for deployments that already
enable tracing; it does not require deploying a trace backend.

### 1.2 Flush job logs while the job is running

**Today.** `JobLogHandler` in `ops/sink.py` buffers records until artifact
completion or the worker's final cleanup. A hung crawl therefore has an empty
log panel until it ends.

**Change.** Flush pending records on the existing throttled progress cadence,
at most once per second per job through the worker's existing database pool,
never once per log line. Preserve the capacity/drop behaviour and always perform
a final flush. A database outage must not fail the crawl; retain the bounded
buffer and retry on the next cadence.

### 1.3 Preserve bounded structured context in `job_events.data`

**Today.** `ops/sink.py` appends log-derived rows with `data=None`, discarding
contextvars and record extras immediately before the database write.

**Change.** Persist a small allowlisted payload. Start with `source`, `scraper`,
`host`, `request_id`, and `trace_id`; add fields only when the UI or an incident
query uses them. Scrub the payload before persistence, cap serialized values,
and never persist arbitrary objects or an unrestricted copy of
`record.__dict__`.

Allowlisting is preferable to attempting to remove every unsafe or oversized
foreign logging field after the fact.

---

## Phase 2 — Add service request visibility

### 2.1 Add one raw-ASGI middleware to both Python services

`catalogue-control` and `catalogue-service` both disable uvicorn access logs;
neither currently replaces them with a complete structured request log.

Use raw ASGI middleware, not `BaseHTTPMiddleware`, because control's
`/v1/events` response must stream incrementally. After routing, emit one
`http.request` log containing:

* service, method, route template, status, and duration;
* request ID and active trace ID when present;
* resolved operator identity in logs only.

Generate or validate `x-request-id` at the first service boundary, echo it in
the response, and forward it on internal calls. The explorer should forward an
incoming request ID or create one. This gives an operator one correlation value
without requiring distributed tracing or a schema migration.

Exclude successful `/health` and `/metrics` requests from access logs. Log an
SSE connection when it closes or fails, including its lifetime and final
status. Do not treat an hours-long successful SSE connection as ordinary API
latency.

### 2.2 Record the small API RED metric set

Both services expose:

```text
catalogue_http_requests_total{service,method,route,status_class}
catalogue_http_request_duration_seconds{service,method,route}
catalogue_http_requests_in_flight{service}
```

Use route templates, never concrete paths. `status_class` is one of `2xx`,
`3xx`, `4xx`, or `5xx`; individual status codes are unnecessary initially.
Reuse `metrics.DURATION_BUCKETS`, with SSE excluded from the duration histogram.

`catalogue-service` also gets the same thin `telemetry.py` re-export used by
control, logging configuration in `__main__`, and a `/metrics` route beside
`/health`.

### 2.3 Make explorer errors reportable

Add `catalogue-explorer/src/hooks.server.ts` with `handleError` and `handle`.
Log the full server-side error against the request ID and return only a safe
title, detail, and that ID to the browser. Add `+error.svelte` to display the
ID. Existing `ControlError` RFC 9457 fields should be preserved where safe.

---

## Phase 3 — Make metrics truthful and deliberately small

### 3.1 Fix snapshot gauge semantics first

The control `/metrics` handler reads job states, workers, and sources from
Postgres and writes them into a process-global registry. That registry never
deletes a label series. If a worker disappears from `WORKERS`, or the last job
leaves a state, the old value can remain forever.

Add a registry operation that atomically replaces all series in a gauge family,
or render database-backed families directly from the current query results.
Each scrape must:

* emit every fixed job state, including zero;
* remove workers and sources absent from the current snapshot;
* fail the scrape, or expose a clear scrape-error metric, when the database
  snapshot cannot be read. Silently returning old values is misleading.

Prefer aggregate worker metrics over a `worker=<uuid>` series. Worker UUIDs
churn and are useful in logs and the roster, not in long-lived metrics.

### 3.2 Audit defined instruments before adding any

The registry defines outbound requests, request duration, cache outcomes,
browser renders, host backoff, HTTP errors, and parse failures, but these need
verified production call sites. For each existing instrument, record:

| Instrument | Production call site | Bounded labels | Operator question | Keep? |
|---|---|---|---|---|
| outbound requests/errors | fetch transport boundary | source, normalized host, outcome | Is a source failing because a shop blocks or times out? | wire up |
| outbound duration | fetch transport boundary | normalized host | Which upstream is slow? | wire up |
| cache outcomes | cache boundary | outcome | Is a supposedly fresh run replaying cached responses? | wire up |
| browser renders | renderer boundary | source | Which sources use the expensive browser path? | wire up |
| host backoff/lease contention | limiter/lease boundary | normalized host | Are we being throttled or capacity-limited? | keep |
| parse failures | extractor boundary | source, schema field | Which bounded contract field is drifting? | keep only if field is an enum/allowlist |

Delete unused definitions rather than presenting them as available signals.

### 3.3 Core operational metric contract

This is the initial complete set. Existing proxy and pipeline accounting
metrics may remain, but they do not expand this alerting scope.

| Operator question | Metric | Labels/source |
|---|---|---|
| Is work accumulating? | `catalogue_jobs{state}` | current DB snapshot; fixed states |
| How long has work waited? | `catalogue_queue_oldest_age_seconds` | DB snapshot; no labels |
| Can queued work run? | `catalogue_workers{health}` | aggregate `healthy`/`lost` counts from DB |
| Are jobs succeeding? | `catalogue_jobs_completed_total` | `source`, bounded `outcome` |
| Are jobs getting slower? | `catalogue_job_duration_seconds` | `source`, bounded `outcome` |
| Is a scheduled source late? | `catalogue_source_overdue_seconds` | `source`; enabled scheduled sources only |
| Has a source never succeeded? | `catalogue_source_success_state` | `source`; `0` until first usable result, then `1` |
| Did output shrink materially? | `catalogue_source_records` and `catalogue_source_record_ratio` | latest usable job versus previous, by `source` |
| Are upstreams failing or slow? | existing request/error/duration families | bounded source, normalized host, outcome |
| Is either API unhealthy? | Phase 2 RED metrics | service, method, route template, status class |

`catalogue_source_overdue_seconds` must be schedule-aware. Compute zero until
the next expected completion plus a source-specific grace period, then report
the excess. Paused and disabled sources do not contribute. A source with no
successful result must still be represented by `catalogue_source_success_state`.

`catalogue_source_record_ratio` is updated only from usable complete results.
Truncated, interrupted, failed, or intentionally partial results must not make a
healthy source appear to have lost most of its catalogue.

### 3.4 Define how worker-local metrics are scraped

Prometheus must discover and scrape each worker endpoint separately. Counter
resets on restart are expected and handled with `rate()`/`increase()`. Do not
merge endpoints behind a load balancer, because successive scrapes could hit
different registries.

If the deployment cannot provide stable per-worker scrape targets, keep
authoritative fleet metrics in control from Postgres and omit worker-local
counters until discovery exists. Do not pretend a scrape of one arbitrary
worker represents the fleet.

---

## Phase 4 — Consume metrics with a bounded deployment

Start with one metrics backend. Distributed tracing and a trace backend are not
part of the initial rollout.

Add an optional compose profile containing Prometheus and, if a graphical view
is wanted, Grafana. Configure a short initial retention such as 14 days and an
explicit storage-size limit. The default developer `docker compose up` remains
unchanged.

### 4.1 One dashboard

The first dashboard has only four sections:

1. **Freshness:** overdue seconds, never-successful sources, latest record count,
   and record ratio.
2. **Work:** jobs by state, oldest queued age, healthy/lost worker counts, and job
   completion outcomes.
3. **Upstreams:** request rate, failure outcomes, p50/p95 latency, cache ratio,
   browser renders, backoff, and lease contention.
4. **Services:** request rate, 5xx rate, p50/p95 latency, and scrape health by
   service.

Every panel links to the relevant operator page or includes the source/route
needed to find the associated logs.

### 4.2 Initial alerts

Keep alerts few and actionable. Thresholds below are starting points and must be
tuned against actual schedules and runtimes.

| Alert | Initial condition | Operator action |
|---|---|---|
| SourceNeverSucceeded | enabled scheduled source has success state `0` after its first expected run plus grace | inspect its latest job and logs |
| SourceOverdue | overdue exceeds 15 minutes for 10 minutes | inspect schedule, latest job, and upstream health |
| QueueWithoutCapacity | queued jobs exist and healthy workers are zero for 5 minutes | restore or resume workers |
| QueueStuck | oldest queued age exceeds 30 minutes while healthy workers exist | inspect leases, host contention, and worker logs |
| JobFailuresHigh | failure/degraded ratio exceeds 25% over one hour with a minimum of four completions | group failures by source before paging |
| MetricsTargetDown | a production target is not scraped for 5 minutes | restore the service or scrape path |

Do not alert on an individual worker UUID or raw heartbeat age. The actionable
condition is loss of capacity while work exists. Every alert gets a one-paragraph
runbook and a link to the relevant dashboard section.

---

## Phase 5 — Complete the operator loop

### 5.1 Expose existing log filters

`GET /v1/jobs/{id}/logs` supports `level` and `q`, but the explorer proxy drops
both. Pass them through and add level and text controls to the job page.

### 5.2 Surface correlation IDs

Show the request ID on error pages with a copy control. Show job ID and run ID
prominently on job details; show a trace ID only when tracing is enabled. These
are log-search keys, not metric dimensions.

### 5.3 Keep polling until it is a measured problem

Once job logs flush every second, cursor polling is adequate for the initial
operator experience. Do not add a new SSE `job.log` topic until measurements
show that polling load or latency is a problem. The paged endpoint remains the
source of truth for history and reconnection.

---

## Deferred — distributed tracing

The current root job/run spans may continue to exist, and trace IDs may be
included in logs when available. Do not yet add a `traceparent` schema column,
queue propagation, collector, or trace backend.

Revisit distributed tracing only after metrics and correlated logs are in use
and an incident demonstrates a question they cannot answer. At that point,
document the question, sampling policy, retention, expected span volume, and
retry/link semantics before implementing cross-process propagation.

---

## Small logging hygiene fixes

* Run `_redact_event` once per record, after `format_exc_info`, in both the
  structlog and foreign-record chains. Redacting before exception formatting
  leaks secrets carried only by exception messages; recursive passes before and
  after formatting waste work without improving other fields.
* Confirm runtime credentials, especially proxy passwords in userinfo URLs, are
  registered before the first connection log. Keep the userinfo regex as a
  second layer.

---

## Order of work

| Order | Work | Exit condition |
|---|---|---|
| 1 | Phase 1 and log filtering from 5.1 | running job logs are live, structured, scrubbed, and searchable |
| 2 | Phase 2 | both APIs emit bounded request logs and RED metrics; errors show a request ID |
| 3 | Phase 3.1 and 3.2 | snapshot gauges cannot go stale; every retained instrument has a real call site |
| 4 | Phase 3.3 and 3.4 | the metric contract is implemented and every worker target is accounted for |
| 5 | Phase 4 | one bounded backend, one dashboard, and the initial actionable alerts work |
| 6 | Phase 5.2 | correlation keys are visible to operators |
| later | distributed tracing or log SSE | only with a measured operational need |

The first success criterion is deliberately modest: an operator can tell that a
source is late, see whether work and healthy capacity exist, find the failing
job, and read its current structured logs. Anything beyond that must earn its
ongoing cost by answering a question this path cannot.
