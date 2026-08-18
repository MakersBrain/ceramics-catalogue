# Database Queue Migration Plan

Status: implemented on `nats-queue-migration`; production cutover pending
Queue backend: NATS JetStream
Scope: replace PostgreSQL work delivery without rewriting catalogue storage or the operations control plane

## Objective

Move source-job delivery out of PostgreSQL into a real message broker that can run locally in Docker Compose, on an ordinary VM, or through a managed service later.

The first migration deliberately keeps PostgreSQL as the authoritative store for run and job state. It does not initially move catalogue data, schedules, events, progress, notifications, worker records, or host-concurrency coordination.

The resulting boundary should be:

| Concern | Initial owner after migration |
|---|---|
| Deliver the next job | Message broker |
| Run/job status and attempt accounting | PostgreSQL |
| Host and shared-edge concurrency | PostgreSQL |
| Worker lifecycle and control flags | PostgreSQL |
| Events, progress, notifications and run closure | PostgreSQL |
| Catalogue records and observations | PostgreSQL |

The broker answers “what should a worker attempt next?” PostgreSQL answers “is this delivery current, and what is true about this job?”

## Current semantics that must be preserved

The current database queue is more than a `FOR UPDATE SKIP LOCKED` claim. A replacement must preserve all of these behaviours:

- Claiming a job does not consume an attempt.
- An attempt begins only after the worker acquires every required host/shared-edge slot and starts the scrape.
- Host contention requeues with a short delay without consuming an attempt.
- Workers advertise required capabilities, including exact browser backends.
- A job can discover that it requires a browser and move to a compatible worker without consuming another attempt.
- The selected browser backend is stable across retries unless an operator explicitly resets the lineage.
- A worker periodically reports liveness and extends both its job delivery and host leases.
- Worker death eventually makes unfinished work available to another worker.
- Pause and cancel requests reach a running worker during its heartbeat.
- SIGTERM drains safely and returns unfinished work.
- Finishing the last non-terminal job closes and summarises the run exactly once.
- Events are durable and replayable; progress remains mutable level state rather than an event per update.

The existing implementations are primarily in:

- `catalogue-dump/src/mb_ceramics_catalogue/ops/queue.py`
- `catalogue-dump/src/mb_ceramics_catalogue/ops/leases.py`
- `catalogue-dump/src/mb_ceramics_catalogue/ops/runs.py`
- `catalogue-dump/src/mb_ceramics_catalogue/ops/worker.py`

## Options considered

### NATS JetStream — recommended

JetStream provides file-backed durable streams, shared pull consumers, explicit acknowledgements, negative acknowledgements with delay, acknowledgement deadlines, in-progress deadline extensions, redelivery, and publication deduplication.

It fits this workload well because:

- The server is small and straightforward to run in Compose.
- The Python worker is already asynchronous.
- Pull consumers let each worker request work only when a local execution slot is free.
- In-progress acknowledgements map naturally to the existing five-second worker heartbeat.
- A single durable consumer can be shared horizontally by every worker eligible for a route.
- The same protocol works locally, on a VM, in a NATS cluster, or with a managed NATS provider.

Trade-offs:

- Capability routing must use deliberately disjoint subjects and consumers.
- Priority should initially use a small number of priority bands rather than relying on arbitrary numeric broker priority.
- NATS must not be treated as an exactly-once transaction coordinator for PostgreSQL side effects.

References:

- <https://docs.nats.io/nats-concepts/jetstream/consumers>
- <https://docs.nats.io/using-nats/developer/develop_jetstream/model_deep_dive>

### RabbitMQ quorum queues — strong alternative

RabbitMQ provides mature exchanges, routing keys, publisher confirms, manual acknowledgements, strict priority, dead-lettering, delayed retry, and an established management UI.

It is a strong choice when routing flexibility and standard AMQP support matter more than operational simplicity. The main costs are a heavier service to operate and careful configuration of acknowledgement timeouts for browser jobs that can run for hours.

Reference: <https://www.rabbitmq.com/docs/quorum-queues>

### Redis Streams — possible, not preferred

Redis Streams provides consumer groups, pending-entry tracking, explicit acknowledgement, and stale-delivery recovery through `XAUTOCLAIM`.

It would require the application to implement more policy itself: delayed delivery, capability routing, priority, dead-letter handling, delivery heartbeat conventions, and cleanup. That recreates too much of the queue inside this codebase.

Reference: <https://redis.io/docs/latest/develop/data-types/streams/>

### Temporal — reconsider only for broader workflow orchestration

Temporal directly models long-running activities, heartbeat-based failure detection, retries, cancellation, timers, and workflow completion. It could eventually replace much of the current run control plane.

It is not a bounded queue migration. Self-hosting adds a larger service and its own persistent database, and adopting it would rewrite scheduling, run state, retry policy and cancellation together. Reconsider it only if catalogue runs become substantially more complex workflows.

Reference: <https://docs.temporal.io/>

### Cloud-specific queues

Cloudflare Queues, SQS and similar managed products are not part of this migration. NATS can already run locally, on a VM, as a cluster, or through a managed NATS service, so no second queue provider or portability adapter is planned.

## NATS queue interface

Use a narrow `NatsJobQueue` boundary owned by the operations package. It isolates JetStream client calls from worker lifecycle logic and makes them testable; no provider abstraction is shipped:

```python
class NatsJobQueue:
    async def connect(self) -> None: ...
    async def provision(self) -> None: ...
    async def publish(self, job: JobEnvelope) -> None: ...
    async def next_delivery(self, routes: Sequence[str]) -> Delivery | None: ...
    async def stats(self) -> BrokerStats: ...
    async def close(self) -> None: ...

class Delivery:
    async def in_progress(self) -> None: ...
    async def ack(self) -> None: ...
    async def retry(self, delay: float) -> None: ...
    async def reject(self) -> None: ...
```

`NatsJobQueue` is the sole implementation. There is no `PostgresJobQueue`, backend selector, provider column, dual-provider execution mode, or worker compatibility path. Attempt accounting, source settings, control flags and terminal-state decisions remain in the PostgreSQL job-state layer rather than in the queue interface.

## Message contract

Messages must be small, versioned references rather than snapshots of all job configuration:

```json
{
  "schema": "catalogue.job.v1",
  "job_id": "32f9dfc6-…",
  "run_id": "9bfb08aa-…",
  "source_id": "ceradel",
  "generation": 1,
  "route": "plain.normal",
  "priority": 100,
  "enqueued_at": "2026-08-18T12:00:00Z"
}
```

Do not include proxy credentials, provider tokens, full run parameters, or source configuration. A worker loads current authoritative configuration after it validates the message.

`generation` is a fencing value. Pause, retry, capability escalation, lineage reset, or any other operation that invalidates an existing delivery increments the generation. A worker receiving an older generation acknowledges it without executing. A delivery is eligible only when this comparison succeeds atomically:

```text
message.generation = jobs.delivery_generation
```

## NATS stream and routing design

Create a file-backed stream named `CATALOGUE_JOBS` with work-queue retention and subjects under `catalogue.jobs.v1.>`.

Initial disjoint routes:

```text
catalogue.jobs.v1.plain.normal
catalogue.jobs.v1.browser.auto.normal
catalogue.jobs.v1.browser.camoufox.normal
catalogue.jobs.v1.browser.cdp_extension_proxy.normal
```

Add `high` and `backfill` priority bands only when there is a demonstrated need:

```text
catalogue.jobs.v1.plain.high
catalogue.jobs.v1.plain.normal
catalogue.jobs.v1.plain.backfill
```

Each subject is owned by one durable pull consumer shared by all eligible worker processes. Work-queue consumers must not have overlapping subject filters.

A browser worker may consume its browser routes and, when those are empty, also pull from the shared plain consumer. A plain worker never binds to browser consumers.

Use explicit acknowledgements. Configure an acknowledgement interval longer than the worker heartbeat, and send in-progress acknowledgements while a job runs. Keep broker maximum delivery high or unlimited because broker deliveries do not equal application attempts.

## Reliable publication: transactional outbox

Creating database state and publishing a broker message cannot be one atomic transaction. Use a transactional outbox:

1. Creating or resuming a job writes `runs`, `jobs`, and `queue_outbox` in one PostgreSQL transaction.
2. A dispatcher selects unpublished outbox rows and publishes them to JetStream.
3. The publish uses `job_id:generation` as the stable broker message ID.
4. The dispatcher waits for broker persistence confirmation.
5. It marks the outbox record published.
6. Repeating any step is safe.

Add a unique database constraint on `(job_id, generation)` so concurrent resume/retry operations cannot create two logical publications. JetStream's message-ID deduplication window is finite, so correctness must come from database generation fencing and idempotent state transitions rather than assuming the broker will suppress duplicates forever.

Suggested outbox columns:

```text
id
job_id
generation
route
priority
available_at
payload
created_at
published_at
publish_attempts
last_error
```

The outbox is not the work queue: workers never claim from it. It is a small, transient record of broker publications that must eventually happen.

Add a reconciliation command that recreates missing messages for every eligible non-terminal job. This makes PostgreSQL sufficient to rebuild JetStream after complete broker-volume loss.

## Worker delivery lifecycle

### Receive and validate

1. Pull a message only when the process has a free local job slot.
2. Validate its schema and route.
3. In one database transaction, read the job, its source settings, generation, attempts and execution lease.
4. ACK immediately if the job is missing, terminal, cancelled, or from an obsolete generation.
5. ACK a job-paused delivery. The pause operation already invalidates its generation, and resume publishes a new generation.
6. If the source is paused, atomically invalidate this generation without making it terminal, then ACK. Source resume republishes every eligible queued job for that source through the outbox.
7. If the source is disabled, atomically mark the job `skipped`, emit its terminal event, participate in run closure, then ACK.
8. Delay the delivery only when `scheduled_for` or another time-based eligibility condition is in the future.

Do not leave paused work parked as an unacknowledged broker delivery, and do not repeatedly NAK it. Pause is durable database state; resume is a new publication.

### Reserve the execution

Before acquiring any host slot, atomically reserve the current job generation in PostgreSQL without consuming an attempt:

1. Compare delivery generation, state, source eligibility and capability requirements.
2. If another unexpired execution owns the job, NAK until after that lease's expiry with bounded jitter.
3. If an earlier execution lease expired and `attempt >= max_attempts` without `resume_without_attempt`, atomically mark the job failed, release its host leases, emit the failure/notification, close the run if appropriate, then ACK the broker delivery.
4. Otherwise set `state='leased'`, `lease_owner`, `lease_expires_at`, and a new random `execution_token`, without incrementing `attempt`.
5. Return the reservation and exact capabilities selected for this execution.

Only the worker holding the current execution token may acquire or release host slots, start, heartbeat, write final source results, or make a terminal job transition. A stale worker must fail every compare-and-set even if it still has the same job ID and delivery generation.

This reservation replaces queue discovery by `FOR UPDATE SKIP LOCKED`; it does not discover work. It is a point lookup and fencing transition for a broker delivery.

### Acquire coordination and start

1. Acquire the source host slot and any shared-edge slot for `(job_id, execution_token)` using the current PostgreSQL implementation extended with execution-token fencing.
2. If either is busy, release only slots owned by that execution token, conditionally clear that token's job reservation, and NAK with a 30-second delay.
3. Do not consume an application attempt for host contention.
4. Atomically transition the still-current reservation to `running` and consume an attempt. The update must match job ID, generation, lease owner, execution token, state and unexpired lease.
5. If the start compare-and-set fails, release only this execution token's slots and resolve the broker delivery according to the newly read authoritative state.

Add `execution_token` to `host_leases`, and scope renew/release operations by both job ID and execution token. `release_all(job_id)` is unsafe after moving to an at-least-once broker because two duplicate deliveries can share a job ID; a losing duplicate must never release the winning execution's slots.

PostgreSQL execution leases remain as fencing, but workers no longer scan them to discover work. If an acknowledgement deadline expires while the original worker is still running, a second delivery sees the unexpired reservation and cannot acquire host slots or start the same generation.

### Execute and heartbeat

During the crawl heartbeat:

- Send JetStream `in_progress` for the delivery.
- Extend only the PostgreSQL execution and host leases matching this execution token.
- Update worker liveness.
- Read cancel, pause, drain and proxy-revocation flags.

If database heartbeats fail until the execution lease's safety margin is reached, the worker must stop scraping and enter the safe interruption path even when broker heartbeats still succeed. Extending the broker deadline cannot authorize work after the database execution fence may have expired.

Keeping broker and database liveness in the same heartbeat preserves the current control latency.

### Finish and acknowledge

1. Commit source results, terminal job state, final progress, artifact metadata, token-scoped host-slot release, durable events and possible run closure in PostgreSQL. Every job-owned write must prove the current execution token before committing.
2. Only after that transaction commits, send a confirmed broker ACK.
3. If the ACK is lost, a later delivery sees a terminal job and ACKs without repeating the scrape or load.

This is intentionally at-least-once delivery with idempotent state transitions. Do not claim global exactly-once behaviour across NATS and PostgreSQL.

## Control operations

### Cancel

- Queued: mark the job cancelled. A later delivery sees terminal state and ACKs.
- Running: set the existing cancel flag. The worker observes it during heartbeat, stops safely, commits cancellation, then ACKs.

Removing a particular message from the broker is not required for correctness.

### Pause and resume

- Pausing a queued job increments its generation and marks it paused. The stale message is harmless.
- Pausing a running job follows the existing partial-artifact path, commits `paused`, and ACKs the delivery.
- Resuming increments the generation and creates a new outbox publication with `resume_without_attempt` semantics.

This avoids leaving an unacknowledged broker delivery parked indefinitely.

### Pause, resume and disable a source

- Pausing a source sets the source flag, requests safe interruption of running jobs, and increments the generation of every queued or merely leased job for that source. No replacement message is published while the source remains paused.
- A delivery racing with source pause performs the same conditional invalidation if needed, then ACKs.
- Resuming a source publishes every eligible queued job for that source through the outbox. It does not resume jobs individually paused by an operator.
- Disabling a source marks its non-running queued jobs `skipped`, safely interrupts running jobs according to the existing control policy, emits terminal events, and reevaluates affected run closure.

These source-setting changes and their generation/outbox or terminal-state changes must occur in the same database transaction.

### Explicit retry

Reset the application retry state, increment the delivery generation, and insert a new outbox row in the same transaction.

### Capability escalation

Update the job requirements and generation, insert a new outbox row on the compatible route, then ACK the old delivery. The outbox ensures that a crash between the database transition and publication cannot lose the job.

### Browser backend selection and recovery

- A `browser.auto` delivery may be reserved only by a worker advertising at least one exact backend capability.
- During the execution-reservation transaction, deterministically select and persist the exact backend before acquiring host slots.
- Once persisted, that exact backend is lineage for every recovery and retry.
- If an auto-route delivery reaches a worker that does not advertise the already-selected backend, atomically increment the generation, insert an outbox record for `browser.<selected_backend>`, and ACK the auto delivery.
- Exact-route consumers validate that their subject and advertised capability match the persisted backend before reservation.

This prevents a crashed `browser.auto` job from repeatedly reaching workers that cannot continue its selected lineage.

### Worker drain

- Stop pulling new deliveries.
- Ask every running task to complete its existing safe interruption path.
- ACK jobs committed as paused/cancelled/terminal.
- NAK unstarted deliveries immediately.
- Close the broker connection only after all delivery handles are resolved.

## Migration phases

### Phase 0 — record invariants and baseline

- Add an architecture decision record for broker choice and responsibility boundaries.
- Capture current queue depth, claim latency, retry counts, lease expirations and run duration.
- Turn every semantic requirement in this document into a job-state, NATS-delivery or end-to-end test.
- Record broker recovery and the forward-fix procedure before changing production.

Exit criteria: current queue behaviour is expressed by tests that the NATS implementation must pass.

### Phase 1 — separate state transitions from queue discovery

- Split queue delivery mechanics from PostgreSQL job-state transitions.
- Add the `JobQueue` interface and NATS delivery/message types.
- Convert claim/start/release into point-lookup state transitions driven by a broker delivery.
- Remove worker calls that scan PostgreSQL for queued work.
- Keep the old code only in branch history; do not ship a PostgreSQL queue adapter or runtime backend switch.

Exit criteria: unit and database tests prove the new reservation, fencing and terminalisation transitions independently of a running broker.

### Phase 2 — add local JetStream and the outbox

- Add a pinned NATS image with JetStream to Docker Compose.
- Add a persistent data volume, authentication and a health check.
- Provision the stream and durable consumers idempotently.
- Add `nats-py` as a required runtime dependency.
- Add delivery generation, execution token and the transactional outbox schema.
- Add execution-token ownership to host leases and every job-owned state transition.
- Implement the dispatcher, `NatsJobQueue` and reconciliation command.
- Move expired-execution terminalisation into a state reconciler that never discovers new work.
- Add broker and outbox metrics.

Exit criteria: messages survive broker and publisher restarts, duplicate publication is harmless, and broker loss can be reconstructed from PostgreSQL.

### Phase 3 — staging and failure injection

- Deploy the complete NATS path in an isolated staging Compose project using a copy or disposable instance of catalogue state.
- Run an explicit allow-list of low-risk plain sources, followed by browser sources, entirely through NATS.
- Exercise manual runs, scheduled runs, retry, cancel, pause, resume, duplicate publication, worker death, database interruption, broker restart and complete JetStream-volume reconstruction.
- Run no PostgreSQL queue workers in this environment.

Exit criteria: several staging runs complete with correct state, artifacts, notifications and run summaries, and every failure-injection test passes.

### Phase 4 — production cutover

This is a one-way deployment cutover, not a per-job dual-backend migration:

1. Disable schedules and operator creation of new runs.
2. Drain the existing PostgreSQL queue workers and require zero leased/running jobs.
3. Take a verified PostgreSQL backup and record the old deployment image digests.
4. Deploy and provision JetStream, but do not start consumers yet.
5. Apply the additive schema migration for generations, execution tokens, token-scoped host leases and the outbox.
6. In one migration transaction, create outbox records for every eligible queued job and preserve paused/cancelled/terminal states.
7. Deploy the new control service, dispatcher and NATS-only workers from this branch.
8. Verify outbox drain, broker depth, worker reservations and a manual low-risk run.
9. Re-enable the daily and weekly schedules.

The old worker image must not be running after step 5 because it could still discover work directly from PostgreSQL.

Exit criteria: at least one complete weekly cycle uses JetStream, with no lost, duplicated, or permanently stuck jobs.

### Phase 5 — remove obsolete queue code

- Delete `FOR UPDATE SKIP LOCKED` claiming, PostgreSQL idle polling, and the old queue-coupled reaper on the NATS branch.
- Retain the state reconciler for expired execution leases, attempt exhaustion, terminal events and run closure. It maintains authoritative state but never hands work to a worker.
- Keep job rows, execution fencing, host leases, scheduler leadership, events and run closure in PostgreSQL.

Exit criteria: PostgreSQL is not polled to discover queued work.

### Phase 6 — optional later extractions

Evaluate independently rather than bundling them into the delivery migration:

- Move immutable artifacts to R2/S3-compatible object storage.
- Replace PostgreSQL host leases with a dedicated coordination primitive.
- Move live events to a broker stream while retaining a durable event authority.
- Reconsider Temporal if run orchestration grows beyond source fan-out and aggregation.

## Observability

Add metrics for:

- Outbox records pending, failed and oldest age.
- Broker messages ready, in flight and redelivered by route.
- Oldest ready-message age by route.
- Publish and acknowledgement failures.
- Generation mismatches/stale messages.
- Deliveries ACKed because the job was already terminal.
- Host-contention delayed deliveries.
- Execution-lease conflicts after broker redelivery.
- Expired executions recovered and exhausted executions terminalised.
- Source-pause invalidations and resume republications.
- Auto-browser deliveries rerouted to their selected exact backend.
- Invalid messages terminated and surfaced through the critical queue alert.
- Reconciliation publications.

The operations UI should continue reading authoritative job state from PostgreSQL. Broker depth supplements that view; it does not replace it.

## Failure and recovery behaviour

| Failure | Expected result |
|---|---|
| Control process dies after database commit but before publish | Outbox dispatcher publishes later |
| Dispatcher publishes twice | Stable message ID and generation make it harmless |
| Worker dies before start | Delivery deadline expires; no application attempt consumed |
| Duplicate deliveries arrive together | One execution reservation wins; all others delay without acquiring host slots |
| Worker dies during scrape | Broker redelivers; after lease expiry one new execution token may recover it |
| Worker dies during its final allowed attempt | State reconciler or redelivery marks it failed, emits the notification and closes the run |
| Worker commits success but ACK is lost | Redelivery observes terminal state and ACKs |
| NATS is unavailable | Outbox accumulates; run/job state is not lost |
| PostgreSQL is unavailable before start | Workers extend or delay broker delivery but do not reserve or start work |
| PostgreSQL becomes unavailable during work | Worker stops before its database execution lease can expire and resolves through the safe interruption path |
| JetStream volume is lost | Reconciliation republishes eligible non-terminal jobs from PostgreSQL |
| Stale paused/retried message arrives | Generation mismatch causes ACK without execution |
| Source is paused while a message is in flight | Generation is invalidated, the delivery is ACKed, and resume republishes later |
| Auto-browser message reaches the wrong exact worker | It is transactionally rerouted to the selected backend and ACKed |
| Invalid or unsupported message arrives | Terminate/reject it and raise an operator notification |

## Recovery and deployment reversal

There is no live queue-provider rollback and no worker backward-compatibility mode. Once production jobs have been published to NATS, recovery is forward through NATS:

- Restart or repair JetStream and let the outbox drain.
- Rebuild a lost JetStream volume by reconciling eligible non-terminal jobs from PostgreSQL.
- Redeploy a corrected NATS worker/control image when application code is faulty.
- Keep schema migrations additive and forward-compatible with the NATS application during the cutover.

Before production cutover, abandoning the branch and redeploying the current release is sufficient because no production state has changed.

After production cutover, redeploying the previous PostgreSQL-queue worker is not a supported rollback: it could claim rows already represented by NATS messages and execute them twice. If an unrecoverable emergency makes a full reversal unavoidable, stop all publishers and NATS consumers, revoke their credentials, preserve the broker volume, and restore both the pre-cutover PostgreSQL backup and previous deployment together. Any post-cutover catalogue changes are lost in that disaster-recovery operation, which is why fixing forward is the normal path.

## Acceptance criteria

The migration is complete only when automated tests prove:

- A crash at every point between job creation, outbox creation, publication and publish confirmation loses no job.
- Duplicate publication never causes duplicate terminal output.
- Two simultaneous deliveries of one generation produce one execution reservation and never release each other's host slots.
- A worker killed before `start` consumes no attempt.
- A worker killed during scraping is safely redelivered.
- A worker killed during its final attempt becomes terminal and cannot leave its run open.
- A lost final ACK does not repeat catalogue loading.
- Host and shared-edge contention consume no attempt.
- Plain workers never receive browser-only work.
- Runtime browser escalation reaches a compatible worker.
- Exact browser-backend lineage survives recovery and retry.
- Pause, resume, cancel and explicit retry preserve current semantics.
- Source pause invalidates queued deliveries, source resume republishes them, and source disable terminalises affected queued jobs.
- An auto-browser delivery with existing exact lineage is rerouted when received by an incompatible worker.
- SIGTERM resolves every held delivery safely.
- Two workers finishing sibling jobs concurrently close their run once.
- Scheduled-run idempotency is unchanged.
- Complete JetStream data loss can be reconstructed from PostgreSQL.
- Queue depth, oldest age, redelivery and outbox backlog are observable and alertable.

Run the PostgreSQL job-state suites for reservation/fencing transitions plus Compose-level NATS delivery, duplicate, crash and restart tests. There is no adapter compatibility matrix.

## Estimated delivery

For one engineer familiar with the current operations code:

| Work | Estimate |
|---|---:|
| Interfaces and invariant tests | 2–3 days |
| JetStream Compose, queue client and provisioning | 2–3 days |
| Outbox, generation fencing and reconciliation | 3–4 days |
| Execution reservations and token-scoped host leases | 3–5 days |
| Control actions and worker lifecycle integration | 3–4 days |
| Canary, failure injection, metrics and runbook | 3–5 days |

Expected implementation and staging-validation period: approximately three to five engineering weeks. Do not combine catalogue storage migration with this schedule.

## Decision

Proceed directly with NATS JetStream, a narrow NATS queue interface, a transactional outbox, and PostgreSQL generation fencing on the dedicated migration branch.

Do not implement a PostgreSQL queue adapter, runtime backend selector, dual-provider canary, or backward-compatible worker. PostgreSQL remains the job-state and coordination authority, but NATS is the only work-delivery system. Further database extraction should be planned as separate changes with their own failure models and recovery procedures.
