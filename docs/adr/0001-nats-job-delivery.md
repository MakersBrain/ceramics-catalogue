# ADR 0001: NATS JetStream for job delivery

Status: accepted
Date: 2026-08-18

## Context

Workers previously discovered work by polling and claiming PostgreSQL rows.
The queue lifecycle had become coupled to database scanning, while the system
needed durable delivery that works in local Compose, ordinary VMs, and a
managed deployment without adopting a cloud-specific API.

## Decision

Use NATS JetStream as the sole job-delivery backend. Use file-backed work-queue
retention, disjoint durable pull consumers, explicit acknowledgements,
in-progress heartbeats, delayed negative acknowledgements, and stable
`job_id:generation` publication IDs.

Keep PostgreSQL authoritative for runs, jobs, attempts, schedules, source
controls, events, progress, host concurrency, and execution fencing. Publish
through a transactional outbox. A broker delivery authorizes no work until an
exact generation has atomically reserved a fresh PostgreSQL execution token.

There is no PostgreSQL queue adapter, runtime provider selector, dual-write
worker, or backwards-compatible claim path.

## Consequences

- Broker delivery is at least once; generation and token compare-and-set make
  duplicates safe.
- NATS becomes an operated dependency with authentication, durable storage,
  monitoring, backup/reconstruction procedures, and health checks.
- PostgreSQL remains required during execution and is not reduced to catalogue
  storage in this change.
- Runtime rollback to the database queue is unsupported after cutover. Normal
  recovery fixes forward; disaster reversal restores the old deployment and a
  matching pre-cutover PostgreSQL backup together.
- A later move of artifacts, coordination, events, or orchestration is a
  separate architectural decision.
