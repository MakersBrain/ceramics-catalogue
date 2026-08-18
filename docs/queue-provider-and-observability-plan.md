# Queue Provider and Observability Portability Plan

Status: proposed
Current provider: NATS JetStream
First portability target: Cloudflare Queues HTTP pull consumers

## 1. Outcome

Make work delivery and queue observability provider-neutral without restoring a
PostgreSQL database queue or maintaining two active delivery paths.

The system must keep these properties:

- NATS remains the only production implementation until another provider passes
  the same delivery contract and failure tests.
- Exactly one provider is active in an environment. Switching is a maintenance
  operation, not a per-job decision and not a dual-write canary.
- PostgreSQL remains authoritative for job state, delivery generation,
  execution fencing, attempts, cancellation, pause state, and run completion.
- The transactional outbox remains the boundary between a committed job and an
  external broker publish.
- Workers remain safe under at-least-once delivery and duplicate messages.
- `/v1/queue`, the Ops panel, and `/metrics` expose one stable semantic model.
  Provider-specific details may be added, but callers do not need to understand
  JetStream to determine whether work is stuck.
- Local development works with NATS, and provider contract tests work with an
  in-process deterministic fake. Cloudflare credentials must never be required
  to run the default local stack or default test suite.

This is not a plan to move PostgreSQL to D1. D1 is a separate state-store
migration with different SQL and transaction concerns.

## 2. Why one large `Queue` interface is the wrong boundary

Publishing, consuming, provisioning, and observing have different permissions
and deployment locations:

- the dispatcher publishes;
- workers consume and acknowledge;
- deployment tooling provisions queues and retention policy;
- the control service reads operational statistics;
- Prometheus exports normalized statistics.

A single object with every method would give the control service write/consume
credentials and force every provider to pretend it supports every JetStream
feature. Use four narrow interfaces instead.

## 3. Provider-neutral vocabulary

The application contract uses these terms:

- **route**: a stable application capability lane, currently
  `plain.normal`, `browser.auto.normal`, `browser.camoufox.normal`, and
  `browser.cdp_extension_proxy.normal`;
- **envelope**: the versioned job reference already fenced by job id and
  delivery generation;
- **delivery**: one provider lease for one envelope;
- **ready**: available for a compatible consumer;
- **in flight**: delivered but not acknowledged where the provider can report
  it;
- **redelivered**: delivered more than once where the provider can report it;
- **backlog**: all unacknowledged messages according to the provider;
- **outbox pending**: committed database rows not yet confirmed published;
- **exact**, **best effort**, or **unsupported**: the quality of a statistic.

Do not call a Cloudflare queue a stream, a lease id a NATS acknowledgement, or
an approximate backlog an exact value in the public API.

## 4. Delivery interfaces

Create `mb_ceramics_catalogue.ops.delivery` and move the application-owned
types there. Provider implementations live below `ops.providers`.

Illustrative signatures:

```python
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class JobEnvelope:
    schema: str
    job_id: UUID
    run_id: UUID
    source_id: str
    generation: int
    route: str
    priority: int
    enqueued_at: datetime

    @property
    def deduplication_key(self) -> str:
        return f"{self.job_id}:{self.generation}"


@dataclass(frozen=True)
class PublishReceipt:
    provider_message_id: str | None
    duplicate: bool | None


class QueuePublisher(Protocol):
    async def connect(self) -> None: ...
    async def publish(self, envelope: JobEnvelope) -> PublishReceipt: ...
    async def close(self) -> None: ...


class Delivery(Protocol):
    envelope: JobEnvelope
    provider_message_id: str | None
    delivery_attempt: int | None
    remaining_delivery_attempts: int | None
    lease_deadline: datetime | None

    async def acknowledge(self) -> None: ...
    async def retry(self, delay_seconds: float) -> None: ...
    async def reject(self, reason: str) -> None: ...
    async def extend(self, seconds: float) -> bool: ...


class QueueConsumer(Protocol):
    async def connect(self) -> None: ...
    def deliveries(self, routes: Sequence[str]) -> AsyncIterator[Delivery]: ...
    async def close(self) -> None: ...


class QueueProvisioner(Protocol):
    async def validate(self, routes: Sequence[str]) -> list[str]: ...
    async def apply(self, routes: Sequence[str]) -> None: ...
```

`remaining_delivery_attempts` is `None` when the provider has no finite delivery
limit or cannot report it. A finite provider must expose it so the worker and
operations UI can warn before exhaustion.

`extend()` returns `False` when a provider cannot renew a live lease. The worker
must then verify at startup that the configured initial visibility timeout
covers the complete time for which it can hold a delivery:

```text
visibility_seconds >
    maximum_source_timeout
  + maximum_pre_execution_wait
  + maximum_load_and_finalization_time
  + shutdown_and_ack_margin
```

The bound and its margin must be named settings, validated, and exercised by a
test. A no-op extension that returns success is forbidden.

`reject()` means “do not deliver this invalid envelope again.” Providers without
a terminal acknowledgement must send the message to a configured dead-letter
queue or report that they cannot satisfy the contract during validation.

## 5. Keep broker details out of the outbox

The current outbox stores a NATS subject. That makes a committed database row
provider-specific before the dispatcher sees it.

Add provider-neutral columns:

```sql
route             text not null
envelope_schema   text not null default 'catalogue.job.v1'
deduplication_key text not null
```

Migration steps:

1. Backfill `route` from the suffix of the existing NATS subject.
2. Backfill `deduplication_key` as `job_id || ':' || generation`.
3. Update every outbox writer to write the neutral fields.
4. Make the dispatcher derive the provider destination from `route`.
5. Remove `subject` after a release has run with no readers or writers using it.

Do not add a `provider` column to jobs or outbox rows. One environment has one
active provider. At cutover, current eligible generations are reconstructed
from PostgreSQL into the newly selected provider.

## 6. Provider factory and configuration

Add one explicit factory used by dispatcher, worker, control, and administrative
commands:

```python
class QueueProviderName(StrEnum):
    NATS = "nats"
    CLOUDFLARE = "cloudflare"


def publisher(settings: QueueSettings) -> QueuePublisher: ...
def consumer(settings: QueueSettings) -> QueueConsumer: ...
def stats_reader(settings: QueueSettings) -> QueueStatsReader: ...
def provisioner(settings: QueueSettings) -> QueueProvisioner: ...
```

Common configuration:

```text
CATALOGUE_QUEUE_PROVIDER=nats
CATALOGUE_QUEUE_POLL_EMPTY_SECONDS=2
CATALOGUE_QUEUE_VISIBILITY_SECONDS=<validated complete delivery-lifetime bound>
```

NATS-only configuration. Production deployments use role-specific credentials;
the local stack may point every role at the same development token file:

```text
CATALOGUE_NATS_URL=nats://nats:4222
CATALOGUE_NATS_PUBLISH_TOKEN_FILE=/run/secrets/nats-publish-token
CATALOGUE_NATS_CONSUME_TOKEN_FILE=/run/secrets/nats-consume-token
CATALOGUE_NATS_STATS_TOKEN_FILE=/run/secrets/nats-stats-token
CATALOGUE_NATS_ADMIN_TOKEN_FILE=/run/secrets/nats-admin-token
CATALOGUE_NATS_STREAM=CATALOGUE_JOBS
```

Cloudflare-only configuration:

```text
CATALOGUE_CF_ACCOUNT_ID=...
CATALOGUE_CF_PUBLISH_TOKEN_FILE=/run/secrets/cloudflare-queues-publish-token
CATALOGUE_CF_CONSUME_TOKEN_FILE=/run/secrets/cloudflare-queues-consume-token
CATALOGUE_CF_RECOVERY_TOKEN_FILE=/run/secrets/cloudflare-queues-recovery-token
CATALOGUE_CF_STATS_TOKEN_FILE=/run/secrets/cloudflare-queues-stats-token
CATALOGUE_CF_ADMIN_TOKEN_FILE=/run/secrets/cloudflare-queues-admin-token
CATALOGUE_CF_QUEUE_PLAIN_ID=...
CATALOGUE_CF_QUEUE_BROWSER_AUTO_ID=...
CATALOGUE_CF_QUEUE_BROWSER_CAMOUFOX_ID=...
CATALOGUE_CF_QUEUE_BROWSER_CDP_EXTENSION_PROXY_ID=...
CATALOGUE_CF_QUEUE_RECOVERY_DLQ_ID=...
```

Rules:

- Parse and validate only the selected provider's settings.
- Within a process, load only the credentials needed by that role. The control
  service must not receive publish, consume, or administrative credentials; a
  worker must not receive provisioning credentials. The dispatcher receives
  publish and recovery-DLQ credentials, but not ordinary route-consumer or
  administrative credentials.
- Never accept secrets directly in a URL or expose them in exceptions.
- Import optional provider SDKs lazily.
- Fail startup when a route has no destination, acknowledgement is not
  supported, or visibility is shorter than the complete delivery-lifetime
  bound.
- Log the selected provider and route mapping without logging credentials.
- Do not permit provider selection in a run, source, job, or HTTP request.

## 7. NATS implementation

Refactor the existing `NatsJobQueue` without changing behavior:

- one JetStream work-queue stream;
- one filtered durable consumer per route;
- `Nats-Msg-Id = job_id:generation` for publish deduplication;
- explicit confirmed acknowledgement;
- delayed `nak` for retry;
- terminal acknowledgement for invalid envelopes;
- `in_progress()` behind `Delivery.extend()`;
- provisioning isolated in `NatsProvisioner`;
- consumer/stream information isolated in `NatsStatsReader`.

This refactor is successful only if existing duplicate, restart, reconstruction,
acknowledgement-failure, and route-selection tests pass unchanged.

## 8. Cloudflare Queues implementation

Use HTTP pull consumers because the collectors are long-running Python/browser
processes outside Workers. Cloudflare documents concurrent HTTP pull clients,
lease IDs, explicit acknowledgement/retry, and visibility timeouts up to twelve
hours:

- https://developers.cloudflare.com/queues/configuration/pull-consumers/
- https://developers.cloudflare.com/api/resources/queues/subresources/messages/methods/pull/
- https://developers.cloudflare.com/api/resources/queues/subresources/messages/methods/ack/

Use one Cloudflare queue per route. This preserves capability isolation without
inventing client-side filtering that repeatedly leases work a worker cannot do.
Configure all four queues with the recovery DLQ and the highest permitted retry
limit. A finite retry budget is a delivery constraint, not the application's job
attempt policy.

Publishing:

1. Map the envelope route to a queue id.
2. POST the encoded envelope to that queue.
3. Treat only a successful API response as a publish confirmation.
4. Mark the outbox row published after confirmation.
5. Rely on PostgreSQL generation fencing for duplicates; Cloudflare Queues is
   at-least-once and does not provide the same documented publish deduplication
   primitive as `Nats-Msg-Id`.

Consumption:

1. Short-poll only routes supported by the worker.
2. Pull a small batch with the validated visibility timeout covering the full
   delivery-lifetime bound defined above.
3. Decode and validate before reserving the PostgreSQL generation.
4. Hold the provider `lease_id` only in memory.
5. Acknowledge or retry using the lease id.
6. Treat any failed or ambiguous acknowledgement as an uncertain ack: the
   database execution fencing makes a later duplicate harmless. Do not assume
   that crossing the visibility deadline makes an acknowledgement invalid;
   Cloudflare may still accept a late lease acknowledgement.

Cloudflare has a finite provider retry limit. Exhausting it must not strand a
PostgreSQL job whose outbox row is already marked published:

1. Configure a recovery DLQ for every route queue.
2. Add a recovery consumer, run by the dispatcher role, that reads DLQ
   envelopes and locks the referenced PostgreSQL job.
3. If the envelope generation is still current and the job is eligible,
   transactionally increment `delivery_generation` and insert its new outbox
   row, then acknowledge the DLQ message after commit.
4. If the generation is stale or the job is terminal/ineligible, acknowledge
   the DLQ message without redrive.
5. If PostgreSQL is unavailable, leave/retry the DLQ message. Give the recovery
   DLQ a retention alert and an operator command that can reconstruct all
   current eligible generations if the DLQ itself is lost.

The recovery path is not an alternative live provider and does not execute
jobs. It only restores delivery from PostgreSQL authority.

Before implementation, prove whether the current Cloudflare API can extend an
existing pull lease. The currently documented pull and ack APIs do not promise
lease extension. If it cannot, enforce the fixed complete-lifetime bound and
reject configuration beyond Cloudflare's limit.

## 9. Normalized statistics interface

Create a statistics interface independent of consumer/publisher credentials:

```python
from typing import Generic, TypeVar


class Accuracy(StrEnum):
    EXACT = "exact"
    BEST_EFFORT = "best_effort"
    UNSUPPORTED = "unsupported"


T = TypeVar("T")


@dataclass(frozen=True)
class Measurement(Generic[T]):
    value: T | None
    accuracy: Accuracy


@dataclass(frozen=True)
class QueueRouteSnapshot:
    route: str
    ready: Measurement[int]
    in_flight: Measurement[int]
    redelivered: Measurement[int]
    delivered: Measurement[int]
    oldest_age_seconds: Measurement[float]


@dataclass(frozen=True)
class QueueRecoverySnapshot:
    backlog_messages: Measurement[int]
    oldest_age_seconds: Measurement[float]


@dataclass(frozen=True)
class QueueSnapshot:
    provider: str
    observed_at: datetime
    last_success_at: datetime | None
    available: bool
    backlog_messages: Measurement[int]
    backlog_bytes: Measurement[int]
    consumer_count: Measurement[int]
    routes: tuple[QueueRouteSnapshot, ...]
    recovery_dlq: QueueRecoverySnapshot | None
    error: str | None = None


class QueueStatsReader(Protocol):
    async def snapshot(self) -> QueueSnapshot: ...
```

Every measurement has field-level quality. `value=None` with
`accuracy=unsupported` means the provider cannot report that value. When a
collection fails, `available=false`, `observed_at` records the failed attempt,
and `last_success_at` preserves the most recent successful collection. Missing
or unavailable values must never be serialized as zero.

NATS can report exact stream and durable-consumer state through JetStream
consumer information and `/jsz`:

- https://docs.nats.io/running-a-nats-service/nats_admin/monitoring/monitoring_jetstream

Cloudflare realtime queue metrics report backlog count, backlog bytes, and the
oldest message timestamp. Cloudflare describes these metrics as best-effort.
Historical lag, retries, operations, and consumer concurrency come from its
GraphQL Analytics datasets and should not be presented as instantaneous:

- https://developers.cloudflare.com/queues/observability/metrics/
- https://developers.cloudflare.com/api/resources/queues/methods/get_metrics

For Cloudflare, aggregate the four route queues into the top-level snapshot and
retain one route snapshot per queue. Do not synthesize `in_flight`,
`redelivered`, or `delivered` when only backlog is available.

## 10. Stable control API

Evolve `/v1/queue` without making its shape provider-specific:

```json
{
  "at": "...",
  "jobs": {"queued": 3, "leased": 1, "running": 1},
  "eligible": 3,
  "oldest_queued_age_seconds": 42,
  "outbox": {"pending": 0, "ready": 0, "errored": 0},
  "broker": {
    "provider": "nats",
    "available": true,
    "observed_at": "...",
    "last_success_at": "...",
    "backlog_messages": {"value": 4, "accuracy": "exact"},
    "backlog_bytes": {"value": 2048, "accuracy": "exact"},
    "consumer_count": {"value": 4, "accuracy": "exact"},
    "routes": [],
    "recovery_dlq": null
  }
}
```

The control service calls `QueueStatsReader`; it must no longer import NATS or
know a stream name. Database/outbox statistics still return when the provider is
unavailable.

Keep an optional `provider_details` object only for diagnostics that cannot be
normalized. The Ops UI must not depend on it.

## 11. `/metrics` contract

Export stable application metrics from the control service:

```text
catalogue_queue_provider_up{provider="nats"}
catalogue_queue_snapshot_age_seconds{provider="nats"}
catalogue_queue_backlog_messages{provider="nats"}
catalogue_queue_backlog_bytes{provider="nats"}
catalogue_queue_consumers{provider="nats"}
catalogue_queue_route_ready{provider="nats",route="plain.normal"}
catalogue_queue_route_in_flight{provider="nats",route="plain.normal"}
catalogue_queue_route_redelivered{provider="nats",route="plain.normal"}
catalogue_queue_route_delivered{provider="nats",route="plain.normal"}
catalogue_queue_route_oldest_age_seconds{provider="cloudflare",route="plain.normal"}
catalogue_queue_recovery_backlog_messages{provider="cloudflare"}
catalogue_queue_recovery_oldest_age_seconds{provider="cloudflare"}
catalogue_queue_outbox_pending
catalogue_queue_outbox_oldest_age_seconds
```

Metric rules:

- The provider label is bounded to configured providers; never use account,
  queue id, URL, durable name, or exception text as a label.
- Route is bounded to the application route registry.
- Unsupported metrics are omitted, not exported as zero.
- On collection failure, set `provider_up` to zero and preserve a timestamp for
  the last successful snapshot so alerts can distinguish stale from empty.
- Outbox metrics remain provider-independent and authoritative in PostgreSQL.
- Counters emitted by dispatcher/workers (`publish failures`, `ack failures`,
  invalid envelopes) remain process metrics and include the bounded provider
  label.
- A worker emits
  `catalogue_queue_retry_budget_low_total{provider,route}` when a finite
  delivery approaches exhaustion. The stats reader includes the recovery DLQ
  in its cached health snapshot without mixing it into ordinary route backlog.
- Prometheus collection must use a short timeout and cached snapshot. A scrape
  must not make four sequential remote Cloudflare API calls.

Add alerts for:

- provider down while eligible jobs or outbox rows exist;
- snapshot older than two collection intervals;
- outbox pending/oldest age above thresholds;
- broker backlog growing while workers are healthy;
- repeated redelivery where supported;
- a route approaching its finite provider delivery-attempt limit;
- recovery DLQ non-empty or approaching retention;
- retention risk: oldest provider message approaching provider retention.

## 12. Ops panel behavior

Keep the heading `Queue delivery` and show the provider as a status badge.

The panel has three stable columns:

1. PostgreSQL jobs;
2. transactional outbox;
3. selected delivery provider.

UI rules:

- Show `NATS JetStream` or `Cloudflare Queues`, not the generic word “broker”
  alone.
- Render unsupported values as `—` with a tooltip, never `0`.
- Mark best-effort Cloudflare backlog explicitly.
- Show snapshot age and collection errors without hiding database/outbox state.
- Build route rows from the route registry, not provider queue names.
- Continue polling the server-side explorer proxy; provider credentials never
  reach the browser.
- When a provider lacks per-route in-flight/redelivery statistics, hide those
  columns or render them unsupported consistently across the table.

## 13. Local development and tests

NATS remains the default local provider in Docker Compose.

Add a provider contract suite that every implementation runs:

- publish and consume one envelope;
- preserve every envelope field;
- route isolation;
- confirmed acknowledgement removes delivery;
- retry makes work available again after a delay;
- unacknowledged delivery becomes visible again;
- invalid envelope is terminalized/dead-lettered;
- duplicate delivery cannot execute a generation twice;
- uncertain publish acknowledgement is recovered by outbox replay;
- provider restart/outage does not lose authoritative job state, and explicit
  reconstruction restores every current eligible generation;
- finite provider retry exhaustion redrives an eligible job through a new
  generation and cannot strand it behind a published outbox row;
- stats report a value and accuracy for supported measurements and explicit
  unsupported measurements otherwise;
- secrets are absent from logs, API errors, and metrics.

Test layers:

1. Pure protocol/type tests with a deterministic in-memory fake.
2. NATS integration tests against the disposable local JetStream container.
3. Cloudflare HTTP adapter tests against a fake HTTP server that models lease
   expiry, ack warnings, retry delay, rate limits, and partial API failure.
4. Optional `cloudflare-live` tests using dedicated queues and restricted
   credentials; never part of the default test command.
5. Compose cutover rehearsal with real PostgreSQL and NATS, including complete
   deletion of broker state followed by authoritative reconstruction.

The in-memory fake is a test tool, not a selectable production provider.

## 14. Switching procedure

There is no live dual-provider switch.

1. Pause schedules and creation of manual runs.
2. Drain workers and verify zero `leased`/`running` jobs.
3. Verify zero unpublished, uncancelled outbox rows, or record exactly which
   rows remain. Published historical rows are expected and are not "pending".
4. Stop dispatcher and workers.
5. Provision and validate every route in the target provider.
6. Purge every target route queue and its recovery DLQ, then verify they are
   empty. This is mandatory on both cutover and rollback; never reconstruct on
   top of a retained target backlog.
7. Set `CATALOGUE_QUEUE_PROVIDER` and target credentials/mapping.
8. Run dispatcher reconstruction for every currently eligible generation.
9. Start the dispatcher, then workers.
10. Verify `/v1/queue`, `/metrics`, provider backlog, and one synthetic job per
   capability route.
11. Resume schedules and manual runs.

Rollback uses the same forward reconstruction into the previous provider. Do
not replay raw provider messages across systems; reconstruct authoritative
eligible generations from PostgreSQL. The previous provider may retain its
stopped backlog during the rollback window, but it is purged if selected as the
rollback target before reconstruction.

## 15. Implementation phases

### Phase A — normalize observability while staying on NATS

- Add `QueueSnapshot`, `QueueRouteSnapshot`, and `QueueStatsReader`.
- Move the control endpoint's direct NATS calls into `NatsStatsReader`.
- Change the API and Ops panel to provider-neutral names/nullability.
- Add normalized Prometheus gauges and alerts.
- Cache snapshots with bounded timeout and age reporting.

Exit: NATS behavior and panel information are unchanged, and no control/UI code
imports a NATS type.

### Phase B — split delivery interfaces while staying on NATS

- Move envelope and delivery protocols into `ops.delivery`.
- Split current `NatsJobQueue` into publisher, consumer, provisioner, and stats
  reader adapters.
- Introduce the factory with `nats` as the only accepted provider.
- Make the outbox route provider-neutral and remove stored NATS subjects.

Exit: all existing NATS and PostgreSQL integration tests pass with no behavior
change.

### Phase C — Cloudflare adapter behind tests

- Implement REST publisher and HTTP pull consumer.
- Implement ack/retry and explicit lease-expiry handling.
- Implement four-route queue mapping.
- Implement finite-attempt reporting, recovery DLQ provisioning, and
  PostgreSQL-authoritative DLQ redrive.
- Implement realtime best-effort stats and cached GraphQL historical metrics
  only where operationally useful.
- Add fake HTTP contract/chaos tests and optional live tests.

Exit: the provider contract passes; no deployment selects Cloudflare by default.

### Phase D — cutover rehearsal

- Add secrets/configuration templates and provisioning checks.
- Rehearse NATS → Cloudflare → NATS with disposable queues.
- Prove that each target is purged before reconstruction and that retained
  messages from the previous provider cannot participate after rollback.
- Measure idle-poll API cost, processing latency, and maximum job duration
  against visibility limits.
- Validate Ops UI and Prometheus alert behavior with unsupported metrics.

Exit: documented evidence that no committed or active generation is lost or
executed twice during either switch.

### Phase E — optional environment cutover

- Perform the maintenance procedure.
- Keep the previous provider data for the rollback window, but keep its
  dispatcher and consumers stopped.
- Remove it only after backlog, failure, retry, latency, and cost thresholds are
  stable for the agreed observation period.

## 16. Acceptance criteria

- Worker, dispatcher, control, and UI contain no provider conditionals outside
  provider factories/adapters.
- Outbox rows contain routes and envelopes, not NATS subjects or Cloudflare ids.
- One configuration value selects one provider for the whole environment.
- A duplicate from either provider cannot execute the same generation twice.
- Exhausting a provider's finite retry budget cannot strand an eligible job.
- `/v1/queue` remains useful when the provider is down.
- `/metrics` distinguishes down, stale, empty, unsupported, and best-effort.
- The Ops panel never renders unsupported data as zero.
- NATS remains runnable entirely locally.
- Cloudflare adapter tests require no cloud credentials by default.
- A provider switch and switch-back are rehearsed from PostgreSQL authority,
  without dual writes and without a PostgreSQL queue fallback.

## 17. Explicit non-goals

- Reintroducing `PostgresJobQueue`.
- Selecting providers per run, source, job, or route at runtime.
- Active/active publication or consumption across providers.
- Hiding semantic differences behind fabricated statistics.
- Moving collection workers into Cloudflare Workers.
- Treating D1 migration as part of queue-provider portability.
