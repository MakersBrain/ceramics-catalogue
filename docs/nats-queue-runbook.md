# NATS queue operations

NATS JetStream is the only work-delivery backend. PostgreSQL remains the source
of truth for job state, attempt budgets, execution fencing, controls, events,
and host concurrency. Do not start a pre-migration worker against a database
that has crossed this cutover.

## Local startup

Set a non-default `CATALOGUE_NATS_TOKEN` outside development, then run:

```sh
docker compose up -d nats postgres
docker compose run --rm migrate
docker compose up -d dispatcher worker worker-browser
```

NATS client and monitoring ports bind to loopback by default (`4222` and
`8222`). JetStream data lives in the `natsdata` named volume. Workers and the
dispatcher share `CATALOGUE_NATS_URL`, `CATALOGUE_NATS_TOKEN`, and
`CATALOGUE_NATS_STREAM`. The versioned `catalogue.jobs.v1` subject contract is
fixed by the application.

## Production cutover

1. Pause run creation and scheduled firing.
2. Drain every old worker; require zero `leased` or `running` jobs.
3. Back up PostgreSQL and preserve the deployment identifiers together.
4. Apply `catalogue-ops-schema-v4.sql` and provision the database roles again.
5. Start NATS, then the dispatcher, then NATS workers.
6. Run `catalogue-dispatcher --reconstruct --once` once. This republishes every
   current eligible generation and is safe with NATS message-ID deduplication.
7. Resume scheduling and run creation. Verify a plain and browser job before
   restoring normal concurrency.

The branch intentionally has no PostgreSQL queue adapter, backend selector, or
dual-provider mode. After step 5, recovery is fix-forward.

## Expected failure behaviour

- A dispatcher crash before publishing leaves an unpublished outbox row.
- A crash after publish but before marking the row can publish twice; the
  `job_id:generation` message ID and PostgreSQL reservation fencing make it safe.
- A worker crash leaves an unacknowledged message. JetStream redelivers it and
  the state reconciler clears the expired execution token.
- A duplicate delivery for a live execution is negatively acknowledged with a
  delay. A stale generation is acknowledged without executing.
- Host leases are scoped to the execution token; a losing duplicate cannot
  release the winning execution's slot.

## Broker-state recovery

If the NATS volume is lost, leave workers stopped, recreate/start NATS, and run:

```sh
catalogue-dispatcher --reconstruct --once
```

Then start workers. Reconstruction resets publication only for the current
generation of eligible non-terminal jobs. It does not revive paused, disabled,
cancelled, skipped, or otherwise terminal work.

If PostgreSQL is unavailable, keep the dispatcher and workers stopped until it
returns: a NATS message alone never authorizes execution.

## Disaster reversal

Redeploying the old PostgreSQL-claim worker against a post-cutover database is
unsafe because the same jobs may already exist in JetStream. A full reversal
requires stopping and revoking all NATS publishers/consumers and restoring the
pre-cutover PostgreSQL backup together with the old deployment. Post-cutover
state is lost, so this is disaster recovery rather than routine rollback.
