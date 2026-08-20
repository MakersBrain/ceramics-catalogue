# Performance, retention, and Decodo operations

This is the operator companion to `PERFORMANCE_COST_PROXY_PLAN.md`. All byte
budgets are decimal bytes. Proxy routing remains disabled unless every rollout
gate below has been completed deliberately.

## Release and database migration

1. Stop starting new catalogue runs and allow claimed jobs to drain.
2. Back up PostgreSQL before applying schema or deleting duplicate history:

   ```sh
   timestamp=$(date +%Y%m%d_%H%M%S)
   docker compose exec -T postgres pg_dump -U catalogue -Fc ateliera > "backups/catalogue_${timestamp}.dump"
   ```

3. Back up the job/artifact metadata and preserve the `dumps` volume. Verify
   the dump with `pg_restore --list` before continuing.
4. Apply the additive, ordered migrations:

   ```sh
   docker compose --profile maintenance run --rm migrate
   ```

5. Compare current offers and a sample of price histories before and after the
   migration. New columns and views are additive; no old table or index is
   removed by this release.
6. Run compaction in bounded report-only mode first:

   ```sh
   docker compose --profile maintenance run --rm --entrypoint catalogue-compact migrate --batch 5000
   ```

   Execute one bounded batch at a time only after reviewing counts.
7. Ordinary deletes do not return disk to the filesystem. Use normal vacuum to
   make space reusable inside PostgreSQL. Schedule `VACUUM FULL` only with
   accepted downtime and enough temporary disk for a table rewrite.

Rollback is to disable workers, restore the verified database dump, and deploy
the previous images. Readers accept both legacy `.ndjson` and compressed
`.ndjson.gz`, so compressed artifacts do not need conversion during rollback.

## Artifact and response-cache lifecycle

Artifacts are immutable gzip streams whose SHA-256 covers the compressed bytes.
Retention is intentionally report-only for at least seven days:

```sh
catalogue-maintain --dsn "$CATALOGUE_DSN" \
  --artifacts /var/lib/catalogue/dumps --cache /var/lib/catalogue/cache
```

Save each daily report with its job IDs, file count, and bytes. Only after seven
consecutive reviewed reports should the same command be run with `--execute`.
The selector preserves the latest two successful artifacts per source, failed
or cancelled jobs for 30 days, other successes for 14 days, and every shared
path still referenced by a retained job. It marks the database reference
unavailable before unlinking a file.

The response cache may be pruned because it is reproducible. Golden fixtures
must never share the production cache root.

## Benchmarking

Capture a JSON report before and after a rollout:

```sh
catalogue-benchmark --dsn "$CATALOGUE_DSN" --base-url http://explorer:3000 --output benchmark.json
```

Review wall time, worker-hours, collection/artifact/load/promotion time,
per-source requests/renders/errors/bytes, database and relation sizes, and cold
and warm `/`, `/explore`, and `/ops` timings. Live timing is evidence, not a
deterministic CI assertion. The targets are `/explore` below 750 ms warm and a
daily run below 90 minutes at the current source count.

Capture representative `EXPLAIN (ANALYZE, BUFFERS)` plans after migration and
before adding indexes. The generated facet columns avoid repeated JSON
extraction; do not index every attribute speculatively.

## Decodo secrets and billing ledger

Product: Decodo Residential Proxies. Purchased allowance: 3,000,000,000 bytes
per exact billing cycle. Operational ceiling: 2,400,000,000 bytes. Pilot:
300,000,000 bytes. Daily allocation: at most 80,000,000 bytes and dynamically
lower near renewal. Default job reservation: 25,000,000 bytes.

`decodo.env` contains only `DECODO_API_KEY`, is mode `0600`, is gitignored, and
is used for provider statistics. Gateway credentials belong in a separate
mode-`0600` JSON file outside the repository, selected with
`DECODO_PROFILE_SECRET_FILE`. Example shape, with placeholders only:

Workers stage these private host bind mounts as worker-owned mode-`0400` files
under `/run/catalogue-secrets`, then drop root before catalogue code starts.
This keeps mode-`0600` host secrets usable without broadening their permissions.

```json
{"decodo":{"host":"gateway.example","port":10000,"username":"operator","password":"rotated-secret"}}
```

Never reuse credentials exported in a dashboard CSV after that file was exposed
or copied with broad permissions. Rotate them first. Never put an authenticated
proxy URL in source JSON, run parameters, logs, exceptions, traces, artifacts,
or the database.

Record the dashboard-confirmed UTC cycle start and end in deployment settings.
With proxy routing still disabled, query Decodo and open the fail-closed ledger:

```sh
catalogue-proxy-budget open --api-secret /run/secrets/decodo.env \
  --cycle-start 'YYYY-MM-DDTHH:MM:SS+00:00' \
  --cycle-end 'YYYY-MM-DDTHH:MM:SS+00:00'
```

Reconcile hourly while proxied work is active and after every proxied job/run:

```sh
catalogue-proxy-budget reconcile --api-secret /run/secrets/decodo.env \
  --cycle-start 'YYYY-MM-DDTHH:MM:SS+00:00' \
  --cycle-end 'YYYY-MM-DDTHH:MM:SS+00:00'
```

Keep Decodo auto top-up disabled and its 80%/100% alerts enabled. A failed or
stale reconciliation, exhausted reservation, or 2.4 GB ceiling denies new paid
traffic. The provider's 3 GB cap is the final backstop.

## Proxy pilot and rollback

Keep `CATALOGUE_PROXY_ENABLED=false` until rotated gateway credentials exist,
the exact billing cycle is reconciled, and two candidates have current evidence
of 403/406, a transport refusal, or a 200 block page. Zero parsed rows alone is
not eligibility.

For one short HTTP candidate and one browser candidate, run one bounded 25 MB
sample per justified mode with identical limits and pacing. Review records,
failure rate, estimated and provider bytes, latency, wall time, unexpected exit
changes, and localization differences before authorizing another sample. The
whole pilot is non-renewing and capped at 300 MB.

Promote a source to proxy fallback only after at least three bounded runs show
correct or materially improved records, lower failure rates, no localization
corruption, and a sustainable allocation. Do not start with a long stateful
source.

Emergency rollback requires no image build: set
`CATALOGUE_PROXY_ENABLED=false` and restart workers. The ledger kill switch also
denies every new lease. Existing hostname/shared-edge leases, delays, robots
policy, retry handling, and source concurrency continue to apply under a proxy.
