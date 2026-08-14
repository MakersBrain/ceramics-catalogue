# Proxy manager operations

The proxy manager is fail-closed. A fresh deployment does not spend Decodo
traffic: proxy routing, provider mutations, and the paid probe all default off,
and every new billing cycle starts with its database kill switch active.

## Prepare

1. Keep `decodo.env` mode `0600` with only `DECODO_API_KEY=...`. It is mounted
   into `catalogue-control`; workers never receive it.
2. Generate operator assertion keys:

   ```sh
   uv run --project catalogue-control python scripts/generate-proxy-operator-keys.py
   ```

3. Set the three database role passwords, operator admin/viewer allow-lists,
   and key paths shown in `.env.example`. The identity header must be injected
   by the trusted access proxy; never accept it from direct public traffic.
4. Back up PostgreSQL and the external legacy profile secret separately.

`docker compose up` applies additive migrations, provisions the distinct
migration/control/worker/archive roles, initializes the private profile volume,
and then starts runtime services. Control can append but not update/delete audit
rows. Workers cannot read that table. Only explorer has the assertion private
key; control has the public set.

## First activation

1. Leave all three proxy gates false and deploy.
2. In `/ops/proxies`, compare the subscription dates and raw traffic limit to
   the Decodo dashboard. Decodo's public schema omits the unit of
   `traffic_limit`; only after confirming that `3` means 3 decimal GB set
   `CATALOGUE_PROXY_PROVIDER_LIMIT_UNIT=decimal_gb`.
3. Propose the provider cycle, review every boundary and ceiling, then open it.
   Opening leaves the kill switch active.
4. Reconcile for at least 24 hours and compare local totals with the dashboard.
   Local provider snapshots are monotonic; never repair drift by lowering them.
5. Enable `CATALOGUE_PROXY_MUTATIONS_ENABLED`, create a sub-user allocation no
   larger than the unallocated 2.4 GB operational envelope, and create a route.
   Saving a route performs no network request.
6. Enable `CATALOGUE_PROXY_ENABLED`, reconcile again, then clear the kill switch.
   Start only the bounded pilot. Keep source policy at `never` or `fallback`
   until three successful evidence runs allow promotion.

## Paid probe

The paid probe is a separate gate:
`CATALOGUE_PROXY_PAID_PROBE_ENABLED=true`. It only requests
`https://ip.decodo.com/json`, streams at most 1 MB of application data, reserves
a 1.1 MB provider envelope, closes its reservation, and requests reconciliation.
It is paid traffic and requires explicit operator intent. A new session does
not guarantee a new exit IP.

For a terminal-only smoke, use the same bounded server operation:

```sh
uv run --project catalogue-control python scripts/proxy-live-smoke.py \
  --allow-paid-probe --route-id ROUTE_UUID --actor you@example.com \
  --private-key deploy/secrets/operator-private-key.pem
```

## Incident and rollback

1. Press **Stop new paid traffic**. Use lease revocation only when interrupting
   active requests is intended.
2. Set `CATALOGUE_PROXY_ENABLED=false` and restart workers.
3. Disable the provider-mutation and paid-probe gates.
4. Reconcile and inspect the append-only audit/outbox before changing provider
   state manually.
5. Roll back images if necessary, but retain proxy tables, usage snapshots, and
   audit rows. Never lower accounting or delete evidence.

If a provider mutation succeeds but local credential installation fails, the
profile enters `provider_changed_local_failed` and the kill switch activates.
Resolve the actual provider sub-user first, then reinstall or rotate the local
generation; do not guess and repeat an ambiguous mutation.
