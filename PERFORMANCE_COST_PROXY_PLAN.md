# Catalogue performance, cost, and selective proxy plan

Status: proposed  
Prepared: 2026-08-13  
Revised: 2026-08-14 for the existing Decodo 3 GB/month subscription  
Scope: `catalogue-dump`, `catalogue-control`, `catalogue-service`,
`catalogue-explorer`, PostgreSQL, worker deployment, and optional outbound
residential proxies.

## 1. Outcome

Make the daily catalogue run faster and less expensive without weakening its
politeness controls, audit trail, or data correctness.

The work is successful when:

- unchanged catalogue data no longer causes unbounded PostgreSQL growth;
- immutable crawl artifacts remain useful while consuming much less storage;
- an ordinary HTML page that produces no record does not automatically trigger
  an expensive browser render;
- browser and proxy capacity is used only by sources with evidence that they
  need it;
- the unfiltered `/explore` page responds in under 750 ms at the current data
  volume on this host;
- the daily run finishes in under 90 minutes at the current source count, with
  a stretch target of 60 minutes;
- proxy usage has a hard budget, is observable per source, and cannot leak
  credentials into the database, artifacts, logs, or UI;
- Decodo traffic remains within the purchased 3 GB monthly allowance, with an
  internal stop below that limit to absorb delayed provider accounting and
  in-flight requests;
- direct access remains the default and a proxy is never used to increase the
  request rate or evade an explicit prohibition.

## 2. Measured baseline

Measurements below were taken from the live local stack on 2026-08-13.

| Measurement | Baseline |
| --- | ---: |
| PostgreSQL database | 5.6 GiB |
| `raw_records` including indexes | 3.1 GiB |
| `offer_observations` including indexes | 2.2 GiB |
| Worker artifact volume | 5.5 GiB, 667 files |
| Worker response-cache volume | 1.4 GiB, 28,408 files |
| Latest complete daily run wall time | 138.9 minutes |
| Latest daily run aggregate job time | 19.0 worker-hours |
| Latest daily run HTTP requests | 23,024 |
| Latest daily run browser renders | 2,648 |
| Latest daily run records | 86,928 |
| Latest daily run failed jobs | 11 of 87 |
| Price-observation rows sampled globally | 701,800 |
| Distinct product/price-context states | 90,646 (12.9%) |
| `/explore`, warm request | about 2.0 seconds |
| `/`, warm request | about 0.5 seconds |
| `/ops`, warm request | about 0.02 seconds |

Idle application containers consume only about 36-43 MiB each. Merging the
services is therefore not a priority. The expensive dimensions are retained
data, repeated writes, remote requests, and browser work.

## 3. Delivery order

Do the work in this order:

1. Add measurement and budgets.
2. Stop database and artifact growth.
3. Correct browser fallback and source scheduling.
4. Add conditional HTTP refresh.
5. Speed up explorer aggregates.
6. Run a small residential-proxy experiment.
7. Add selective proxy fallback only if the experiment proves it improves a
   specific source.

Do not begin with a platform migration, VPN, additional queue, Redis, or a
rewrite of the scrapers. None addresses the measured bottlenecks.

## 4. Phase 0: measurement and guardrails

### 4.1 Add per-job traffic accounting

Decodo charges for total transferred traffic: request headers and bodies plus
response headers and bodies. The current summaries count requests and renders
only. Add these cumulative counters to
`ScrapeResult`, `job_progress`, and job summaries:

- `http_tx_bytes_estimated` and `http_rx_bytes_estimated`;
- `browser_tx_bytes_estimated` and `browser_rx_bytes_estimated`, using browser
  protocol encoded transfer sizes where available;
- `cache_bytes_read`;
- `proxy_bytes_reserved` and `proxy_bytes_estimated`;
- `direct_requests`, `impersonated_requests`, `browser_requests`, and
  `proxy_requests`;
- outcome counts for `2xx`, `3xx`, `403`, `429`, timeout, block-page, parser
  empty, and browser-gain.

Do not put URLs containing proxy credentials into `ACTIVITY`, structured logs,
OpenTelemetry attributes, exception strings, or database summaries.

Application counters are an immediate safety estimate, not the billing source
of truth. Reconcile them with Decodo usage statistics at least hourly during a
run and after every run. Provider statistics may lag by 10-15 minutes, so do
not release reserved traffic merely because the provider has not reported it
yet.

Provider-reported traffic is an account/billing-cycle value and belongs in the
shared budget ledger, not in an individual job summary. Do not attribute it to
a job unless Decodo supplies a stable dimension that uniquely identifies that
job; concurrent traffic grouped only by time or target is not sufficient.

### 4.2 Establish benchmark commands

Add a checked-in benchmark script that reports:

- total run wall time and worker-hours;
- per-source requests, renders, records, errors, and bytes;
- time spent collecting, writing artifacts, loading, and promoting;
- database size and per-table/index size;
- `/`, `/explore`, and `/ops` response time, with cold and warm readings.

Keep a JSON baseline artifact in CI or operator output, but do not make live
network timing a deterministic test assertion.

### 4.3 Define budgets

Initial production budgets:

- purchased Decodo allowance: 3.0 GB (3,000,000,000 bytes) per billing cycle;
- internal operational ceiling: 2.4 GB per billing cycle, leaving 600 MB for
  provider-reporting lag, measurement error, and already in-flight traffic;
- pilot: 300 MB total within that 2.4 GB ceiling;
- daily allocation: the smaller of 80 MB or the remaining operational
  allowance divided by the remaining days in the billing cycle;
- initial per-job limit: 25 MB. A reviewed override may raise a job's limit,
  but can never override the daily or billing-cycle ceiling;
- Decodo subscription auto top-up: disabled. Enable its 80% and 100% usage
  notifications/webhooks and record the billing-cycle start and renewal date;
- browser renders: alert when a source exceeds 250 or when more than 50% of
  renders add no parsed row;
- artifact storage: 15 GiB soft limit and 20 GiB hard alert;
- database: alert at 10 GiB until retention is stable;
- one source: no more than its existing timeout and host pacing limits merely
  because it uses another egress route.

## 5. Phase 1: PostgreSQL growth and write amplification

This is the highest-value phase.

### 5.1 Store price-state changes instead of daily duplicates

`catalogue.load_record` currently inserts an observation under a unique key of
`(source_product_id, observed_at, context_sha256)`. Because every crawl has a
new timestamp, an unchanged price is inserted again.

Change the history model to a change log with an observed interval:

```sql
alter table catalogue.offer_observations
  add column last_seen_at timestamptz;
```

For each incoming offer:

1. Take a transaction-scoped advisory lock keyed by `source_product_id` before
   reading or writing its state. Locking only the latest row is insufficient
   because no row exists for two concurrent first observations.
2. Select the latest state by `observed_at` while holding that lock.
3. If its `context_sha256` matches, advance `last_seen_at` monotonically; do not
   insert another observation or replace the raw record that established the
   state.
4. If it differs, insert a new observation with both `observed_at` and
   `last_seen_at` set to the incoming timestamp.

Comparison must be against the latest state, not a global unique constraint.
That preserves a real A -> B -> A price transition.

Normal promotion must process artifacts for a source in observation-time
order. Reimporting an already-seen artifact is a no-op. If an incoming record
predates the latest promoted state and is not already represented, quarantine
it as `out_of_order_observation` for an ordered replay; it must not regress the
current state or `last_seen_at`. Historical backfills use the same per-product
lock and replay records in ascending observation time.

Update history responses to expose `last_seen_at`, while keeping
`observed_at` as the start of the state. Current-offer views continue selecting
the latest `observed_at`.

Expected result: the measured observation growth falls by roughly 87% if the
current change rate persists. This estimate concerns row growth, not final disk
reclamation.

### 5.2 Make raw-record deduplication semantic

`record_sha256` currently hashes the complete record, including `fetched_at`.
Consequently the statement that reimporting the same JSON is idempotent does not
hold across crawls.

Choose and document one of these models:

- Preferred: hash a canonical record with volatile collection metadata such as
  `fetched_at` removed, add `first_seen_at` and `last_seen_at`, and retain one
  row per semantic version.
- Simpler: keep raw rows for 30 days and rely on immutable compressed artifacts
  for long-term audit and reprocessing.

Use the preferred model unless a consumer is found that requires one raw row
for every daily observation.

Before migration, query production consumers and confirm that none relies on
raw-record row count as a run count.

### 5.3 Remove the per-record source update

Inside `load_record`, change:

```sql
insert into catalogue.sources (id)
values (v_source)
on conflict (id) do update set updated_at = now();
```

to `ON CONFLICT DO NOTHING`. Source descriptive metadata is already maintained
through the source-description path. Updating one source row for every product
caused more than 822,000 updates in the measured database.

### 5.4 Reduce source-product updates

The loader rewrites every `source_products` row on each run. First make the
update conditional for large JSON/text columns using `IS DISTINCT FROM`, while
still advancing `last_seen_at` and reactivating the product.

After correctness is established, consider replacing per-row PL/pgSQL calls
with set-based operations over `import_staging`. This is a later optimization:
the measured largest load step was about 15 seconds, so crawl and retention
work has higher priority.

### 5.5 Migration strategy

Use an additive migration rather than editing only the initdb baseline:

1. Add new columns and indexes without deleting old data.
2. Deploy code that writes the new representation.
3. Verify latest-offer and price-history responses against the old views.
4. Backfill `last_seen_at` from each state sequence.
5. Deduplicate in bounded source/product batches.
6. Rebuild or drop obsolete indexes only after query plans are checked.
7. Reclaim disk in a maintenance window using the deployment's approved
   online rebuild method or `VACUUM FULL` if downtime is acceptable.

Take a database backup first. Disk space is not reclaimed merely by deleting
rows, and an in-place table rewrite may temporarily require space comparable to
the table being rewritten.

### 5.6 Acceptance criteria

- Loading the same artifact twice creates no new offer state or raw semantic
  version.
- Loading A, then B, then A creates three chronological state changes.
- `last_seen_at` advances for an unchanged offer.
- Two concurrent first observations cannot create duplicate initial states.
- Reimporting an older artifact cannot regress current state or `last_seen_at`.
- Current API responses are contract-equivalent before and after migration.
- The second load of an unchanged 80-source run grows PostgreSQL by less than
  10% of the first load's growth.
- Existing PostgreSQL and golden tests pass.

## 6. Phase 2: artifact and cache lifecycle

### 6.1 Compress immutable artifacts

Write artifacts as streaming `ndjson.gz` initially. Gzip is already used by
the response cache and requires no new runtime dependency. If CPU or compression
ratio later justifies it, evaluate zstd separately.

Update:

- artifact writing and SHA-256 calculation;
- comparison readers;
- partial-artifact handling;
- control API content handling;
- tests covering path confinement and checksum verification.

Hash the stored compressed bytes so the checksum continues to verify the exact
object on disk. Store an explicit `artifact_encoding` if inference from the
suffix would make migrations ambiguous.

### 6.2 Add artifact retention

Recommended local policy per source:

- keep the latest two successful artifacts indefinitely, because comparison
  needs current and previous;
- keep failed and cancelled artifacts for 30 days;
- keep additional successful artifacts for 14 days;
- optionally archive older monthly snapshots to object storage;
- never delete a file still referenced by one of the retained jobs;
- mark a database artifact reference unavailable before or in the same managed
  operation that removes its file.

Implement a dry-run command that reports file count, bytes, and affected job
IDs. Run it in report-only mode for at least one week before enabling deletion.

### 6.3 Bound the response cache

The cache is reproducible and need not be backed up. Add:

- maximum age for entries not used by golden tests;
- maximum total size with oldest-accessed eviction;
- per-host reporting;
- exclusions for the checked-in/frozen golden-response fixture if it shares
  storage in any deployment.

### 6.4 Acceptance criteria

- Artifact comparison works across compressed files.
- Cancelling a job cannot leave a trusted partial compressed file.
- Retention dry-run and execution choose the same targets.
- Two latest successful artifacts per source remain available.
- A representative artifact set shrinks by at least 70% before retention.

## 7. Phase 3: crawler and browser work

### 7.1 Stop treating parser-empty as browser-required

The generic page crawler currently renders an ordinary HTTP page whenever
parsing returns no rows. Replace that decision with explicit evidence.

Classify the HTTP result as one of:

- usable static document;
- known block/interstitial;
- probable JavaScript shell;
- valid page with unsupported or irrelevant markup;
- transport failure.

Browser fallback is permitted for a block/interstitial, probable JavaScript
shell, or transport refusal. Unsupported markup is a scraper/configuration
problem and must not render hundreds of pages hoping the parser changes.

### 7.2 Measure browser gain

For each fallback, record whether the rendered document:

- produced records where HTTP produced none;
- changed discovery links;
- merely repeated the same empty result;
- failed or timed out.

Add a per-source circuit breaker. Suggested initial behavior:

- after 10 zero-gain renders in one job, stop automatic render fallback;
- keep rendering when the source explicitly sets `render: true`;
- fail clearly with `browser_fallback_no_gain` rather than spending the rest
  of the source timeout.

### 7.3 Repair or reschedule known expensive failures

Prioritize the measured sources:

- `ceram-decor`: 1,156 renders for 215 records;
- `countrylove`: unresolved, 57 renders and zero records;
- `mestrebras`: 140 renders and timeout;
- `hobbyland`: 21 renders and timeout;
- `toepferspass`: unresolved, 10 renders and zero records;
- `cromartie`: unresolved and browser timeout;
- `keramik-kriese`: 146 renders and about 120 minutes.

Until repaired, remove explicitly unresolved zero-output sources from the daily
price schedule or run them weekly/manual. A degraded daily run that repeatedly
performs known futile work is neither fresh nor inexpensive.

### 7.4 Split price refresh from full enrichment

For platforms with structured APIs, add a lightweight daily mode that refreshes
price, currency, availability, and product identity. Run expensive product-page
enrichment weekly or when a product is new or its listing fingerprint changes.

The loader must merge lightweight records without erasing fields that the
weekly enrichment owns. Define field ownership before implementing this mode.

### 7.5 Right-size browser workers after the fixes

The four idle browser worker containers use little memory because Camoufox is
lazy, and the 2.09 GiB image layers are shared across replicas on one Docker
host. Do not optimize replica count based only on image size.

After two weeks of corrected runs:

- calculate maximum concurrent browser jobs and queue delay;
- try two browser replicas with one browser and two pages each;
- retain four only if two increases run wall time beyond the 90-minute target;
- on metered/container hosting, start browser workers around scheduled runs and
  stop them after the browser queue has been empty for a grace period.

## 8. Phase 4: conditional HTTP refresh

The cache already stores `ETag` and `Last-Modified`, but refresh mode ignores
the stored entry.

Add a stale-read path that returns metadata and body without counting it as a
fresh cache hit. On a refresh:

1. Send `If-None-Match` and/or `If-Modified-Since` when present.
2. On `304`, reuse the cached body and advance the cache validation timestamp.
3. On `200`, replace the body and validators atomically.
4. On a transient server failure, follow the source's explicit stale-on-error
   policy; do not silently turn every failure into success from old data.

Track transferred bytes saved by `304` responses. Conditional requests still
count against host rate limits and proxy request/traffic budgets.

## 9. Phase 5: explorer response time

### 9.1 Cache stable aggregate results

Cache the unfiltered facet payload and homepage aggregate for 5-15 minutes in
the explorer process. Invalidate or version it when a run completes promotion.

### 9.2 Correct the homepage offer count

The current homepage counts all historical `offer_observations`. Decide whether
the tile means:

- current offers: count latest/current source-product offers; or
- historical observations: keep the query but label it accordingly.

The current-offer interpretation is likely what a user expects and prevents the
number and query cost from growing merely because another day passed.

### 9.3 Promote hot JSON expressions

Use generated columns or maintained promoted columns for frequently filtered
fields:

- colour name;
- surface;
- form;
- firing maximum;
- normalized package size;
- application methods if array search remains expensive.

Add indexes only after capturing `EXPLAIN (ANALYZE, BUFFERS)` for representative
filters and sorts. Avoid indexing every JSON attribute.

### 9.4 Consider materialized facets last

If caching and promoted columns do not reach the target, maintain a compact
facet-count table refreshed after promotion. Do not refresh it during every
source load; readers should see one coherent catalogue generation.

## 10. Selected residential proxy: Decodo

The project already has Decodo's 3 GB/month residential subscription. Do not
purchase or integrate another provider during this plan. Record the following
non-secret facts in the operator configuration and runbook:

- product: Decodo Residential Proxies;
- allowance: 3 GB per billing cycle;
- advertised plan price checked on 2026-08-14: USD 11.25 plus VAT per month
  (USD 3.75/GB); verify the actual invoice and dashboard before renewal;
- billing-cycle start and renewal date;
- auto top-up disabled and usage notifications enabled;
- credential owner and rotation date, without storing the credential itself.

Decodo documents sticky sessions from 1 to 1,440 minutes, but the exit may
rotate early when the residential peer goes offline. Treat stickiness as a
preference, not a guarantee, and checkpoint long stateful jobs.

Official references:

- [residential pricing](https://decodo.com/proxies/residential-proxies/pricing);
- [quick start and usage statistics](https://help.decodo.com/docs/residential-proxy-quick-start);
- [custom sticky sessions](https://help.decodo.com/docs/residential-proxy-custom-sticky-sessions);
- [traffic accounting](https://help.decodo.com/docs/high-traffic-usage);
- [usage webhooks](https://help.decodo.com/docs/webhooks).

## 11. Selective proxy architecture

### 11.1 Policy

Use a proxy only for a configured source or after a classified direct-access
failure. The fallback ladder is:

1. direct `httpx`;
2. direct TLS impersonation through `curl-cffi`;
3. direct browser when browser evidence exists;
4. sticky residential proxy with the same transport required by the scraper;
5. fail with a classified reason.

Do not automatically proxy parser-empty results, 404s, authentication errors,
robots disallow decisions, or deterministic schema/parse failures.

### 11.2 Configuration model

Add source policy fields:

```text
proxy_policy: never | fallback | always
proxy_profile: logical secret/profile name, not a URL
proxy_country: ISO 3166-1 alpha-2 or null
proxy_session_minutes: bounded integer
proxy_max_megabytes: bounded integer
```

`proxy_profile` resolves in process settings to credentials supplied through a
mounted secret or protected deployment environment. Never accept a literal
authenticated proxy URL through `POST /v1/runs`, schedules, or source JSON.

`proxy_profile`, `proxy_policy=always`, and any limit above the default 25 MB
are operator-only settings. An ordinary run request may reduce a configured
limit or force `proxy_policy=never`, but it may not enable proxying, select a
credential profile, expand the country/session scope, or increase a budget.
Validate these rules at the API boundary as well as in the worker.

Default `proxy_country` to the supplier's country only when the goal is to see
the shop as a normal customer in its own market. Allow an explicit operational
override because country metadata and useful egress are not always identical.

### 11.3 One identity per job

Create a job-scoped `ProxyLease` containing:

- provider/profile;
- endpoint and credentials held only in memory;
- country;
- opaque session ID derived from a random value, not the source name alone;
- creation and expiry times;
- byte/request budget;
- redacted display name.

Use the same sticky session for `httpx`, `curl-cffi`, and Camoufox within the
job. Rotating per request breaks cookies and makes the traffic less coherent.
If a sticky peer disappears, stop or restart from a safe checkpoint with a new
session; do not silently change identity halfway through a stateful sequence.

### 11.4 Transport integration points

- Construct the job's `httpx.AsyncClient` with its native `proxy=` option.
- Pass the same proxy through `curl-cffi`'s `proxy`/`proxy_auth` options.
- Pass a Playwright-style `proxy` object through `AsyncCamoufox` launch options.
- Because the browser is currently shared per process, do not share one browser
  across jobs using different proxy leases. Either key browser instances by
  proxy profile/session or use a dedicated job browser for proxied jobs with a
  strict concurrency cap.
- For proxied browser jobs, block video, audio, fonts, analytics, advertising,
  telemetry, and images by default. Keep documents, scripts, stylesheets,
  XHR/fetch, and scraper-required assets. Source configuration may explicitly
  allow an otherwise blocked resource type or hostname only after a bounded
  test proves it is necessary for correct records.
- Count redirects, service-worker requests, frames, and subresources against
  the same job lease; a navigation is not a single proxy request.

The final point is load-bearing: browser proxy configuration happens at launch.
A process-wide browser cannot safely impersonate several job-scoped egress
identities.

### 11.5 Budget enforcement

Store a durable Decodo budget ledger keyed by billing cycle. It must track the
3 GB purchased allowance, 2.4 GB operational ceiling, daily allocation, pilot
allocation, provider-reported usage, application-estimated usage, and active
reservations. Use decimal bytes throughout this ledger; convert to human units
only for display.

Use the exact billing-cycle boundary shown by Decodo and store it as a UTC
timestamp. Do not reset local usage merely because a calendar month changed.
At renewal, open a new ledger cycle only after the dashboard/API confirms the
new allowance; otherwise remain fail closed.

Before creating a `ProxyLease`, reserve its complete maximum traffic in one
atomic database transaction. The reservation succeeds only if it fits the job,
daily, pilot (while active), and billing-cycle limits simultaneously. This
prevents several workers from each observing the same remaining allowance.
Requests and browser subresources decrement the job reservation. Stop starting
new proxy traffic when the reservation is exhausted; cancel or checkpoint the
job cleanly rather than allowing an unmetered tail.

At job completion, persist estimated upload and download bytes and close the
reservation. Reconcile the ledger with Decodo's provider-reported upload plus
download total at least hourly and after each run. Use the greater of reconciled
provider usage and cumulative application estimates when deciding whether new
leases may start. Never decrease accounted cycle usage automatically; any
correction requires an audited operator action.

At the 2.4 GB operational ceiling, fail closed: deny all new proxy leases and
activate the proxy kill switch. With auto top-up disabled, Decodo's 3 GB plan
limit is the final external backstop. A provider API/dashboard outage must also
prevent new leases unless the last successful reconciliation plus all locally
accounted traffic and reservations is still below the operational ceiling.

### 11.6 Queue and politeness

A proxy does not create permission to increase concurrency. Continue acquiring
the existing hostname lease and shared-edge lease. Additionally:

- cap concurrent jobs per proxy profile/provider;
- preserve Shopify shared-edge pacing even across different proxy exits unless
  evidence justifies a different policy;
- honor `Retry-After`, rate-limit headers, source delay, and circuit breakers;
- enforce the shared Decodo ledger around all proxy traffic;
- never rotate IPs merely to evade a host's rate limit.

### 11.7 Secrets and observability

- Use a mounted secret file or container secret, not `.env`, for provider
  passwords where deployment permits.
- Treat Decodo endpoint-list CSV exports as credentials: never commit them or
  use them as durable runtime configuration. Import the required values into
  the mounted secret, restrict temporary copies to mode `0600`, remove them
  after import, and rotate credentials after any unintended disclosure.
- Redact `userinfo@` from every URL before logging.
- Register a structured-log processor that removes configured secret values.
- Store provider/profile name, country, session age, bytes, latency, and
  outcome—but not endpoint credentials or the exit IP unless operationally
  necessary and retention-controlled.
- Exclude proxy credentials from OpenTelemetry exports.
- Add a kill switch that changes every source to `proxy_policy=never` without a
  new image build.

## 12. Proxy pilot design

### 12.1 Candidate sources

Start with exactly two sources where logs show an actual 403, 429, TLS refusal,
or block page: one short HTTP-oriented source and one browser-oriented source.
Do not include sources whose only symptom is zero parsed rows.

Do not start with `keramik-kriese` or another long stateful job until
checkpoint/restart behavior is implemented. Decodo permits a requested sticky
duration up to 24 hours but explicitly does not guarantee that the residential
peer remains online for that duration.

### 12.2 Experiment matrix

For each candidate, run controlled samples with the same limit and pacing:

- direct HTTP;
- direct impersonated HTTP;
- direct browser if justified;
- the same required transport through a sticky Decodo residential session.

The whole pilot has one non-renewing 300 MB allocation. Each sample initially
receives at most 25 MB. Run one bounded sample per mode and candidate, review
correctness and reconciled Decodo traffic, then authorize the next sample. Do
not automatically execute three full repetitions of every matrix cell. Add a
third candidate only if the first two leave at least 150 MB of pilot allocation
and the added candidate answers a specific unresolved question.

Capture:

- successful product records;
- request and render counts;
- bytes billed/estimated;
- 403/429/block/timeout rate;
- median and p95 request latency;
- source wall time;
- whether the exit changed unexpectedly;
- whether data differs because of geography rather than improved access.

### 12.3 Promotion rule

Enable Decodo for a source only if, across at least three bounded runs:

- record count is correct or materially improved;
- block/failure rate falls materially;
- no meaningful field/price corruption is introduced by localization;
- measured traffic fits a documented recurring source allocation within the
  2.4 GB operational ceiling;
- request volume remains within the existing politeness envelope.

Otherwise leave the source direct and repair its scraper or pause it.

## 13. Testing

Add tests for:

- consecutive observation deduplication and A -> B -> A history;
- raw semantic hashing excluding volatile timestamps;
- compressed artifact round-trip, checksum, partial write, and path safety;
- retention selection and dry-run parity;
- HTTP 304 reuse and stale-on-error behavior;
- browser fallback classification and zero-gain circuit breaker;
- proxy URL redaction for logs and exceptions;
- API rejection of attempts to enable a proxy, select credentials, broaden
  scope, or increase a proxy budget without operator authority;
- direct-by-default routing;
- one sticky session reused across HTTP, impersonated, and browser transports;
- proxy byte-budget cancellation;
- atomic rejection of concurrent leases that would oversubscribe the job,
  daily, pilot, or billing-cycle allocation;
- fail-closed behavior at the operational ceiling and during unsafe provider
  reconciliation outages;
- Decodo reconciliation never lowers accounted usage automatically;
- proxied browsers block nonessential resources and count all allowed
  subresources against the lease;
- no proxy fallback for parser errors, robots decisions, or deterministic 404s;
- source and shared-edge leases still applying under proxies;
- explorer cache invalidation after promotion.

Run the fast, PostgreSQL, and golden suites. Add a local fake CONNECT/HTTP proxy
fixture so CI never needs paid credentials or external proxy traffic.

## 14. Deployment and rollback

### 14.1 Deployment sequence

1. Deploy metrics and redaction first.
2. Back up PostgreSQL and artifact metadata.
3. Deploy additive database schema and dual-compatible readers.
4. Enable change-only writes for one source, then a small group, then all.
5. Enable compressed artifact writes while retaining old-file readers.
6. Run artifact retention in report-only mode for one week.
7. Deploy browser classification with metrics but no automatic source-policy
   changes for the first daily run.
8. Enable conditional refresh.
9. Deploy explorer caching/index changes.
10. Add proxy code with proxy routing globally disabled, reconcile the Decodo
    starting balance, then enable only the two bounded pilot sources.

### 14.2 Rollback properties

- New database columns are additive until the migration is proven.
- Readers accept old and new artifact encodings during the transition.
- Proxy routing can be disabled globally without rebuilding.
- Source proxy/browser overrides can be changed through operational settings.
- Retention remains disabled until dry-run output has been reviewed.
- No old table/index is dropped in the same release that introduces its
  replacement.

## 15. Expected result

The strongest evidence-backed savings are:

- around 87% less ongoing price-observation row growth if current state-change
  frequency persists;
- substantial raw-record reduction by removing volatile timestamps from the
  semantic hash;
- at least 70% artifact reduction from compression before retention;
- removal of hundreds or thousands of low-value browser renders per daily run;
- lower network traffic from conditional requests;
- sub-second cached explorer page loads;
- residential proxy spend limited to the small set of hosts where it measurably
  succeeds, rather than charging for all 23,000+ daily requests;
- Decodo usage stopped at the 2.4 GB internal ceiling and kept within the
  purchased 3 GB billing-cycle allowance even with concurrent workers and
  delayed provider statistics.

The proxy is deliberately last. It is a targeted compatibility tool, not the
foundation of the crawler and not a substitute for fixing parser, storage, or
browser-selection behavior.
