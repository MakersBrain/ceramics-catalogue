# Queue provider operations

PostgreSQL is the authority for job eligibility and generation fencing. NATS
JetStream or Cloudflare Queues transports an envelope; neither provider is a
second job database. Select exactly one provider with
`CATALOGUE_QUEUE_PROVIDER=nats|cloudflare` for control, dispatcher, and every
worker.

## Credentials and provisioning

Production credentials are role-scoped files. NATS uses publish, consume,
stats, and admin token files. Cloudflare uses publish, consume, recovery,
stats, and admin token files. Control receives stats only; workers receive
consume only; the dispatcher receives publish and recovery only. Run
administration from a separate environment that receives the admin credential:

```sh
catalogue-queue-admin apply
catalogue-queue-admin validate
```

For Cloudflare, `apply` configures one HTTP-pull consumer on each of the four
route queues, sets the complete-job visibility timeout and retry limit, connects
each route to the recovery DLQ, and configures a pull consumer for recovery.
For NATS it creates the stream and four filtered durable consumers.

## Provider failure and stale statistics

Open **Operations** and compare provider availability, snapshot time, the
PostgreSQL outbox, and worker health. `unsupported` fields appear as `—` in the
UI and are absent from Prometheus; present measurements carry
`accuracy="exact"` or `accuracy="best_effort"`. Do not interpret an absent
Cloudflare in-flight or redelivery series as zero.

If `QueueProviderDown` fires, verify the role credential and provider endpoint,
then the selected provider value on all services. The control API remains
usable while provider statistics are unavailable. If only
`QueueSnapshotStale` fires, inspect control logs and provider rate limits before
restarting anything.

If `QueueOutboxStuck` fires, stop creating work, inspect dispatcher logs and the
pending outbox count, restore publishing, and let the transactional outbox
drain. Do not delete outbox rows. If `QueueRecoveryBacklog` fires, keep the
dispatcher running: it pulls exhausted Cloudflare messages, validates their
current PostgreSQL generation, creates a new generation/outbox row, then
acknowledges the recovery message.

## Cutover or rollback

There is no dual-write or live provider switch. Use the same sequence in either
direction:

1. Pause schedules and manual run creation.
2. Drain workers; verify zero `leased` and `running` jobs.
3. Verify zero pending uncancelled outbox rows, or record the exact remainder.
4. Stop dispatcher and all workers.
5. Configure the target provider and run `catalogue-queue-admin apply`, then
   `catalogue-queue-admin validate`. Resolve every reported issue.
6. Permanently clear every target route and its recovery DLQ:

   ```sh
   catalogue-queue-admin purge --confirm-provider cloudflare
   # or: --confirm-provider nats
   ```

7. Verify target backlog is zero. Set the new provider and its role credentials
   on control, dispatcher, and workers.
8. Reconstruct only current eligible generations from PostgreSQL:

   ```sh
   catalogue-dispatcher --reconstruct --once
   ```

9. Start the dispatcher, then workers. Verify `/v1/queue`, `/metrics`, and one
   synthetic job for each capability route.
10. Resume schedules and manual runs.

Never reconstruct on top of a retained target backlog and never copy raw
provider messages. Keep the stopped previous provider only for the agreed
rollback window; if selected again, purge it before reconstruction.
