# Operations proxy manager plan

Status: implemented; live rollout remains fail-closed pending the documented dashboard comparison
Prepared: 2026-08-14
Scope: `/ops/proxies`, `catalogue-control`, catalogue workers, PostgreSQL, and
the Decodo Residential Proxies public API.

## 1. Outcome

Add a provider-aware proxy manager to `/ops` that lets an authenticated
operator:

- see Decodo subscription, billing-cycle, provider, application, reservation,
  daily, and per-source traffic in one place;
- reconcile Decodo usage on demand and see whether the local ledger disagrees;
- inspect active and historical proxy reservations and follow them to their
  jobs, runs, and sources;
- create a bounded logical route/session and request/test a new pool selection
  with a paid, explicitly budgeted probe; a different exit IP is not guaranteed;
- create a dedicated Decodo sub-user, set its provider-side traffic limit,
  rotate its password, disable it, or retire it through guarded workflows;
- assign a logical proxy profile and fallback policy to an eligible source;
- stop every new paid lease immediately, without rebuilding an image;
- never reveal or persist a proxy password, API key, or authenticated endpoint
  in PostgreSQL, browser responses, logs, events, traces, or page HTML.

The existing 3 GB subscription remains the final provider limit. The manager
must preserve the 2.4 GB internal operational ceiling, 300 MB non-renewing
pilot allowance, dynamic maximum of 80 MB/day, and 25 MB default per-job
reservation defined in `PERFORMANCE_COST_PROXY_PLAN.md`.

## 2. Terminology: what “new proxy” means with Decodo

The UI must not use one ambiguous **New proxy** button.

Decodo Residential Proxies use a backconnect gateway. Connecting to
`gate.decodo.com:7000` selects an exit from the residential pool. Location,
sticky-session ID, and session duration are encoded as parameters associated
with the proxy username. Generating ten endpoint strings does not purchase or
reserve ten dedicated IP addresses and does not consume traffic by itself.

Expose two separate actions:

1. **New route/session** creates a local, non-secret route specification for an
   existing logical credential profile. It spends nothing until a request is
   sent. A sticky route may keep an exit for the requested duration, but the
   residential device may disappear earlier.
2. **New Decodo sub-user** calls the Decodo Public API, creates credentials,
   installs them in the runtime secret store, and creates a new logical
   profile. This is a privileged provider mutation and requires confirmation.

An optional **Request/test session** action creates a new random session ID and
sends one request to Decodo's fixed IP-check endpoint. It is paid traffic, so it
must obtain a small reservation before the request and account for the result.
It requests a new pool selection, but the residential pool may return an exit
seen previously; neither a new session nor a sticky session guarantees a new or
dedicated IP.

Do not expose subscription purchase, plan upgrade, pay-as-you-go enablement, or
auto top-up in this manager. Those remain dashboard/billing actions outside the
catalogue system.

## 3. Current foundation

Reuse rather than replace the implemented controls:

- `catalogue.proxy_budget_cycles` is the authoritative local billing-cycle
  ledger and already keeps provider usage monotonic.
- `catalogue.proxy_reservations` atomically reserves job, daily, pilot, and
  cycle capacity.
- workers already account proxy requests and estimated bytes, reconcile after
  proxied jobs, and fail closed on unsafe or stale provider state.
- jobs and progress rows already expose `proxy_requests`,
  `proxy_bytes_reserved`, and `proxy_bytes_estimated`.
- source settings already provide a durable operational overlay for ordinary
  crawl parameters; proxy policy needs a separate typed relational overlay and
  worker resolution path.
- `catalogue-control` is the authenticated write service; the explorer reaches
  it server-side and does not expose its bearer token.
- worker logging already registers and redacts runtime secrets.

Missing pieces are provider resource management, provider history snapshots,
an audit trail for proxy administration, read/write control endpoints, secure
dynamic credential distribution, and the `/ops/proxies` UI.

## 4. Safety invariants

These are requirements, not later hardening work:

1. Direct access remains the default. Creating a route or profile never changes
   a source policy and never enables global proxy routing.
2. `CATALOGUE_PROXY_ENABLED=false` remains a deployment-level hard stop. The UI
   may show that state but may not override it.
3. The database kill switch may always be activated. Clearing it is permitted
   only after a fresh successful reconciliation and explicit operator
   confirmation.
4. Provider-reported traffic and local accounted traffic only move upward
   within a billing cycle. No UI action may lower either value.
5. A provider-side sub-user traffic limit must be no larger than the remaining
   local operational allocation assigned to that profile. The sum of every
   managed profile allocation plus an explicitly reserved unmanaged-use
   allocation must not exceed the 2.4 GB operational ceiling. A first dedicated
   catalogue sub-user may receive at most the unallocated remainder, with
   `auto_disable=true`; 2.4 GB is a ceiling, not a per-profile default.
6. Provider total subscription traffic, not merely the catalogue sub-user's
   traffic, remains the cycle backstop because the same Decodo subscription may
   be used elsewhere.
7. Provider API traffic/limit units must be confirmed from the live subscription
   response and normalized at the adapter boundary. Never send the ledger's
   byte value directly to a provider field whose unit is ambiguous.
8. No API accepts an authenticated proxy URL. Requests refer only to provider
   resource IDs, logical profile names, and validated route parameters.
9. The browser never receives API keys, passwords, complete usernames,
   authenticated endpoints, or secret-file paths. Endpoint previews are
   redacted, for example `http://catalogue-…:[REDACTED]@gate.decodo.com:7000`.
10. Provider mutations use generated request IDs and an audit record. Do not
   automatically retry a create, rotate, or delete after an ambiguous timeout.
11. Probe targets are server-defined. Never accept an arbitrary probe URL; that
    would create an authenticated SSRF endpoint and an uncontrolled spending
    path.
12. Existing host leases, retry limits, robots decisions, delays, shared-edge
    pacing, and worker concurrency apply unchanged when a proxy is selected.
13. CI and ordinary tests never contact Decodo or consume paid traffic.

## 5. Operator authentication and control-plane authorization

The hidden `CATALOGUE_CONTROL_TOKEN` authenticates the explorer server to the
control service; by itself it does not authenticate the human using the
browser. Provider mutations are too sensitive to rely only on that hop.

Use one concrete authorization design before exposing `/ops/proxies` mutations:

- protect `/ops` with an explorer-side operator login backed by the deployment's
  identity provider and issue a signed, short-lived, `HttpOnly`, `Secure`,
  `SameSite=Strict` session cookie;
- require an `admin` role for sub-user creation, password rotation, deletion,
  kill-switch clearing, and source proxy enablement;
- permit a `viewer` role for usage, cycles, reservations, and audit history;
- validate `Origin` on every mutating SvelteKit action and keep all writes as
  POST/PUT/DELETE, never GET;
- expire sessions and require recent re-authentication for destructive provider
  mutations;
- have the explorer mint a short-lived signed actor assertion for each control
  request containing actor ID, role, audience, issued/expiry times, and a unique
  nonce; the control service verifies the signature, audience, expiry, nonce,
  HTTP method, and normalized path before authorizing the action;
- keep the existing control bearer token as service authentication, but never
  treat it as operator authorization; control rejects proxy administration
  without a valid actor assertion and enforces `viewer`/`admin` itself;
- derive every audit actor from that verified assertion instead of accepting an
  arbitrary `updated_by` string from form data.

Use Ed25519 assertions: mount the private signing key only in explorer and the
public verification-key set only in control. Assertions expire after at most 60
seconds and are single-use for mutations. Key rotation supports the current and
immediately previous key ID. Other control routes may migrate later, but proxy
administration uses this boundary from its first writable release.

Until both human authentication and control-side assertion verification exist,
ship the page read-only. Do not hide mutation buttons with CSS and call that
authorization.

## 6. Architecture

```text
browser
  │ authenticated /ops session; never sees provider secrets
  ▼
catalogue-explorer /ops/proxies
  │ server-side bearer token
  ▼
catalogue-control
  ├── PostgreSQL: ledger, snapshots, metadata, audit, source policy
  ├── deployment state: same read-only `CATALOGUE_PROXY_ENABLED` value as workers
  ├── decodo.env (read-only): DECODO_API_KEY
  └── proxy-secrets volume (read/write): atomic profiles.json
                  │
                  └── workers (read-only): resolve logical profile per lease

catalogue-control ── HTTPS ── Decodo Public API
workers           ── proxy ── gate.decodo.com:7000
```

Keep provider calls in a provider adapter, not in Starlette route functions.
The control routes validate authority and intent; the adapter owns Decodo
request/response details; the existing proxy module owns reservations and
fail-closed accounting.

### 6.1 Provider interface

Create `mb_ceramics_catalogue.providers.base` with a narrow asynchronous
interface and `providers.decodo.DecodoProvider` implementing it:

- `health()`;
- `subscription()`;
- `traffic(period, group_by, filters, page)`;
- `targets(period, filters, page)`;
- `list_subusers()` and `get_subuser(id)`;
- `subuser_traffic(id, period)`;
- `create_subuser(spec)`;
- `update_subuser(id, change)`;
- `delete_subuser(id)`;
- `list_endpoint_capabilities()`.

Use typed internal models so provider payload changes fail at the adapter
boundary. Preserve the raw response only in process memory for diagnosis; do
not store it wholesale if it may contain credential fields.

Provider-client rules:

- one configured base URL with HTTPS required and no redirects to another
  origin;
- API key only in the `Authorization` header;
- 10-second connect and 30-second total timeout;
- bounded response size and strict JSON parsing;
- retries with jitter only for safe reads and statistics requests;
- no automatic retries for create/update/delete after bytes may have left the
  process;
- map provider errors to stable local codes such as `provider_unavailable`,
  `provider_rejected`, `provider_conflict`, and `provider_ambiguous`;
- register the API key and any generated password with the redactor before the
  first provider call;
- never log request headers, mutation bodies, response bodies, or complete
  URLs containing endpoint-generation credentials.

### 6.2 Do not call the credential-in-query endpoint generator

Decodo documents a custom endpoint generator whose query contains the proxy
username and password and whose response contains authenticated URLs. Calling
it from the manager would put secrets in URL handling, error objects, and
potential access logs.

Generate the route locally from the documented backconnect grammar already
implemented by `ProxyProfile.username_for`. Store only the non-secret route
specification. Construct the authenticated transport object inside the worker
at lease time.

## 7. Secret lifecycle

The existing static external JSON profile file is safe but cannot receive a
new sub-user created through `/ops`. Add a dedicated `proxy-secrets` named
volume:

- mount it read/write only in `catalogue-control`;
- mount it read-only in workers;
- do not mount it in the explorer, service, PostgreSQL, or browser-facing
  containers;
- store one `profiles.json` owned by UID 10001 with mode `0400` and its parent
  directory mode `0700`;
- write a complete temporary file, `fsync`, set ownership/mode, atomically
  rename, and `fsync` the directory;
- initialize the named volume with a one-shot root init container that creates
  and owns the directory correctly, then run control and workers unprivileged;
- serialize writers with a file lock; there should normally be one control
  replica, but correctness must not depend on that;
- validate the completed file with `load_profiles()` before replacing the
  active file;
- keep the API key in the separate read-only `decodo.env` mount;
- seed the volume once from the rotated external credential file, then remove
  the static worker bind mount;
- remove/bypass the worker entrypoint's startup copy for `profiles.json` and
  point `CATALOGUE_PROXY_SECRET_FILE` directly at the read-only shared volume,
  otherwise running workers would never observe a newly installed generation;
- exclude the volume from ordinary database/artifact backups. If credentials
  must be backed up, use a separately encrypted, access-controlled procedure.

On sub-user creation, the control service generates the password with a
cryptographically secure generator satisfying Decodo's current constraints.
The password is sent once to Decodo and installed in the secret volume. It is
never returned to the UI. Return only the logical profile name, provider
resource ID, masked username, and installation status.

Never rotate a password in place while its profile has an active reservation:
Decodo may invalidate the old password immediately, so an in-memory lease is not
a safe compatibility mechanism.

Offer two guarded rotation modes:

1. **Drain and rotate in place** disables new leases for the profile, waits for
   zero active reservations, generates and registers a redacted new password,
   updates Decodo, atomically updates the local secret file, and runs a
   fixed-target health probe under a tiny reservation before re-enabling it.
2. **Blue-green rotation** creates a replacement Decodo sub-user and secret
   generation, atomically redirects routes and new leases to the replacement,
   drains the old profile, then disables and retires the old sub-user. Use this
   mode when uninterrupted availability is required. Allocation checks count
   both profiles during the overlap, so blue-green rotation may require first
   lowering the old provider limit or using reserved rotation headroom.

If a provider change succeeds but the local install fails, mark the operation
`provider_changed_local_failed`, disable the affected profile, activate the
kill switch, and guide the operator through repair. Do not retry with the old
password or assume it remains valid. Every phase is persisted so recovery after
a control-service restart resumes from observed provider and local state rather
than replaying a mutation blindly.

## 8. Database changes

Add an additive `catalogue-ops-schema-v2.sql` migration.

### 8.1 Active billing-cycle ownership

Keep `catalogue.proxy_budget_cycles` authoritative and add:

- a partial unique index allowing only one active cycle per provider;
- explicit lifecycle `proposed`, `active`, `closed`, or `rejected`;
- provider subscription/resource ID and observation evidence;
- `unmanaged_allocation_bytes`, explicitly confirmed for anticipated usage by
  other Decodo users inside the 2.4 GB operational ceiling; the existing 600 MB
  purchased-versus-operational difference remains a separate global margin;
- proposed/confirmed/opened/closed timestamps and verified operator actors.

Control proposes the next cycle from a validated Decodo subscription response.
An admin must confirm exact UTC boundaries, purchased bytes, operational bytes,
daily limit, pilot limit, and unmanaged-use allocation before opening it.
Opening takes the provider advisory lock, requires the previous cycle to be
closed or expired, atomically closes an expired active row before activating the
new row, performs a fresh total-usage reconciliation, and never copies
consumption from the prior cycle. Boundaries cannot be edited after activation.

Workers no longer use `CATALOGUE_PROXY_BILLING_CYCLE_START` or `_END` as runtime
authority. Under the same transaction used to reserve bytes, `reserve()` selects
and locks the single active database cycle covering `now()`. If it is absent,
ambiguous, expired, stale, or unsafe, the lease is denied. The environment
values remain temporary migration assertions only and are removed after the
database path is deployed.

### 8.2 Provider profile metadata

`catalogue.proxy_profiles` contains no secrets:

| Column | Purpose |
| --- | --- |
| `id uuid` | Stable local identifier. |
| `provider text` | Initially `decodo`. |
| `logical_name text unique` | Name used by source settings and leases. |
| `provider_resource_id text` | Decodo sub-user ID. |
| `display_name text` | Operator label, not the provider username. |
| `username_mask text` | Last few safe display characters only. |
| `provider_traffic_limit_bytes bigint` | Provider-side limit last observed. |
| `auto_disable boolean` | Provider-side limit behavior. |
| `enabled boolean` | Whether new routes may select the profile. |
| `secret_generation integer` | Monotonic local credential generation. |
| `secret_installed_at timestamptz` | Presence evidence, never content. |
| `provider_observed_at timestamptz` | Last successful metadata refresh. |
| `created_at`, `updated_at`, `retired_at` | Lifecycle. |

Do not store a username if the provider accepts its immutable ID for all API
operations. If a username is operationally required, keep it only in the secret
file and store a one-way fingerprint plus a masked display value in PostgreSQL.

Store allocations per cycle in `catalogue.proxy_profile_allocations`, keyed by
provider, cycle start, and profile ID, with allocated bytes, timestamps, and
verified actor. Enforce allocation transactionally under the billing-cycle
advisory lock: the sum of enabled/rotating profile allocations plus
`unmanaged_allocation_bytes` on the active cycle cannot exceed
`operational_bytes`. A provider limit may not exceed that profile's active-cycle
allocation. The separate `purchased_bytes - operational_bytes` difference
remains the 600 MB global provider safety margin and is not allocated again. The
provider's total subscription usage remains the reservation backstop even when
unmanaged Decodo users consume traffic.

### 8.3 Usage snapshots

Add `catalogue.proxy_provider_snapshots`:

- provider, cycle start, observed time, grouping dimension and key;
- transmitted bytes, received bytes, total bytes, request count;
- source endpoint (`traffic`, `subuser_traffic`, or `subscription`);
- provider update watermark when available;
- provider bucket start/end and a unique key on provider, cycle, source endpoint,
  grouping dimension/key, and bucket start/end that makes repeated
  reconciliation idempotent; keep `last_observed_at` separately so repeated
  reads update evidence without creating duplicate history.

Retain cycle totals indefinitely because they are billing evidence. Retain
hourly/detail snapshots for 180 days, matching the provider's documented
statistics horizon, then compact them to daily rows.

### 8.4 Managed route specifications

Add `catalogue.proxy_routes` containing:

- ID, label, profile ID, protocol;
- location fields (`country`, optional state/city) after strict validation;
- session mode (`random` or `sticky`) and duration from 1 to 1,440 minutes;
- per-use maximum bytes no greater than 25 MB by default;
- `pilot` and `enabled` flags;
- created/updated/retired timestamps and actor.

Do not store an authenticated URL. A sticky session identifier belongs to a
job lease and is generated when the lease starts; it is not a reusable secret
shown on the page.

### 8.5 Probe history

Add `catalogue.proxy_probes`:

- route and profile IDs; obtain the reservation through the reservation's
  `probe_id` foreign key rather than storing a second circular relationship;
- requested and completed times;
- state and stable error category;
- estimated bytes and provider requests;
- exit country and optionally the observed exit IP with a short retention
  window;
- latency and protocol;
- actor and request ID.

Expire raw exit IP values after seven days or replace them with a keyed hash if
longer trend analysis is needed.

The current reservation table requires a job ID. Generalize it additively for
probes: create the probe row first, make `job_id` nullable, add a nullable
unique `probe_id` foreign key, and enforce `num_nonnulls(job_id, probe_id) = 1`
plus a `purpose in ('job', 'probe')` check. Generalize `reserve()` to accept one
typed consumer. Do not create fake catalogue jobs merely to account a probe.

### 8.6 Source proxy policy and job snapshot

Add `catalogue.source_proxy_policies` rather than placing enablement fields in
the free-form `source_settings.params` JSON:

- `source_id` primary key, policy, route ID, maximum bytes, pilot flag;
- evidence state/count, enabled/disabled timestamps, actor, and revision;
- constraints requiring an enabled route for `fallback`/`always` and a maximum
  no greater than the route maximum and 25 MB default job ceiling.

At job creation, resolve policy in this order:

1. a new checked-in `SourceConfig.proxy_eligible`, default `false`, declares
   whether the source is structurally eligible and supplies safe defaults, but
   does not by itself turn on a paid route;
2. the operator policy selects `never`, `fallback`, or `always` and a route;
3. run/source `CrawlParams` may only narrow the resolved policy to `never` or
   lower the byte maximum.

Snapshot the resolved policy, route/profile IDs, policy revision, location,
session settings, pilot state, and maximum bytes onto the job when it is queued.
Workers lease only from this immutable snapshot, not live `sources.json` or a
policy row that may change mid-job. Existing queued jobs with no snapshot remain
direct. Disabling a route/profile changes policies to `never` and cancels any
not-yet-leased proxied snapshots in the same transaction; running leases drain
unless the explicit emergency-revocation mechanism is used.

Deprecate checked-in `proxy_policy` and `proxy_profile` as runtime authorities.
During migration, list any non-`never` static configurations for explicit admin
review, create relational policies only after confirmation, and then reject
non-`never` static policy on startup. This prevents two independent enablement
sources.

### 8.7 Audit log

Add append-only `catalogue.proxy_admin_audit` with actor, request ID, action,
resource type/ID, timestamp, success, stable error code, and sanitized
before/after JSON. Cover reconcile, kill-switch changes, route creation,
profile creation/rotation/retirement, provider limit changes, probes, and
source-policy changes.

Use separate database roles: a non-login migration owner owns schemas/tables;
control and worker use distinct least-privilege login roles; maintenance uses a
separate archival role. The control role receives INSERT/SELECT but no
UPDATE/DELETE on audit rows, the worker has no direct audit-table privileges,
and neither runtime role owns the table. Add an owner-controlled trigger that
rejects UPDATE/DELETE unless both `current_user` is a member of the maintenance
role and a maintenance-only transaction setting is present. Runtime roles are
not members of that role. The migration process, not a runtime container,
receives the owner credential.

### 8.8 Reconciliation request outbox

Add `catalogue.proxy_reconcile_requests` with request ID, reason, related
reservation/mutation ID, creation time, claimed time, completion time, attempts,
and stable error. Closing a proxied job or probe inserts an idempotent request in
the same transaction as accounting. Provider mutations and kill-switch-clear
attempts do likewise. Control claims requests with `FOR UPDATE SKIP LOCKED`,
coalesces them under the provider advisory lock, and marks every covered request
complete only after totals validate. PostgreSQL `NOTIFY` may wake control early,
but the durable table is the delivery mechanism.

### 8.9 Extend event topics

Add `proxy` to the event-log topic constraint and stream parser. Emit small,
sanitized events such as `proxy.usage_updated`, `proxy.kill_switch_changed`,
`proxy.probe_finished`, and `proxy.profile_changed`. Events carry IDs and
counters, never provider payloads or credential material.

## 9. Control API

Add typed schemas to `catalogue-ops.openapi.json`, regenerate explorer types,
and expose these service-bearer-authenticated routes. Every route additionally
requires a verified viewer/admin actor assertion; mutations require an
appropriate admin assertion unless explicitly documented as viewer-safe.

### 9.1 Read routes

- `GET /v1/proxy/overview` — deployment switch, provider health, subscription,
  active cycle, provider/application/reserved/accounted bytes, remaining
  operational bytes, dynamic daily allowance, reconciliation age/discrepancy,
  pilot state, and kill switch.
- `GET /v1/proxy/cycles?cursor=` — proposed, active, and closed cycle evidence,
  immutable boundaries/allowances, unmanaged allocation, and confirmation
  actors; provider payloads remain sanitized.
- `GET /v1/proxy/usage?from=&to=&group_by=day|source|target|profile` — normalized
  local/provider series with bounded dates and pagination.
- `GET /v1/proxy/reservations?state=&source=&cursor=` — reservations joined to
  jobs/runs/sources.
- `GET /v1/proxy/profiles` — safe metadata plus provider traffic and health.
- `GET /v1/proxy/routes` — non-secret route specifications.
- `GET /v1/proxy/probes?cursor=` — bounded probe history.
- `GET /v1/proxy/audit?cursor=` — sanitized audit history.
- `GET /v1/proxy/candidates` — sources with classified direct failures and no
  deterministic parser/404/robots exclusion, ranked for pilot review.

### 9.2 Ledger actions

- `POST /v1/proxy/reconcile` — fetch provider totals and grouped statistics,
  monotonically update the ledger, write snapshots/audit, and return 202 with
  a request ID. Coalesce concurrent reconciliations with an advisory lock.
- `POST /v1/proxy/kill-switch/activate` — immediate, idempotent, always allowed
  to an admin.
- `POST /v1/proxy/kill-switch/clear` — require deployment proxy support,
  current active cycle, fresh successful reconciliation, usage below every
  ceiling, installed enabled profile, typed confirmation, and recent admin
  authentication.
- `POST /v1/proxy/pilot/start` and `/stop` — never reset pilot consumption;
  starting only changes `pilot_active` after reconciliation.
- `POST /v1/proxy/cycles/propose` — read the Decodo subscription and persist a
  sanitized proposed next cycle without enabling leases.
- `POST /v1/proxy/cycles/{id}/open` — admin-only typed confirmation of immutable
  UTC boundaries and all allowances, followed by locked reconciliation and
  activation.
- `POST /v1/proxy/cycles/{id}/close` — close only an expired cycle with no active
  reservations after final reconciliation; never lower or discard usage.

### 9.3 Profile/sub-user actions

- `POST /v1/proxy/profiles` — create a Decodo sub-user with a generated
  password, active-cycle allocation, provider traffic limit no greater than that
  allocation, `auto_disable=true`, and logical profile.
- `PUT /v1/proxy/profiles/{id}/allocation` — change the active-cycle allocation
  under the provider lock; lowering below active reservations or the provider
  limit requires first draining/lowering them, and raising requires unallocated
  operational capacity and confirmation.
- `PUT /v1/proxy/profiles/{id}/limit` — lower freely within provider semantics;
  raising requires a large enough active-cycle allocation, confirmed units, and
  confirmation.
- `POST /v1/proxy/profiles/{id}/rotate` — drain-first rotation by default, with
  explicitly selected blue-green rotation when capacity permits.
- `POST /v1/proxy/profiles/{id}/disable` — disable locally first, then at the
  provider if supported without deletion.
- `DELETE /v1/proxy/profiles/{id}` — two-stage retirement only when no active
  reservation or source references it; require the logical name as typed
  confirmation.
- `POST /v1/proxy/profiles/refresh` — import safe provider metadata and flag
  drift; never import/display passwords.

Every provider mutation accepts an `Idempotency-Key`/request ID. Enforce a
unique key on actor, action, and idempotency key, and replay the stored terminal
response locally. The audit row is created as `started` before the call and
finalized afterward. Because Decodo mutations do not provide a transaction with
the local database, an ambiguous network result is surfaced for reconciliation
rather than blindly replayed. Creates use a generated unique provider username,
so recovery can list/search provider resources and safely determine whether the
first call succeeded before permitting another attempt.

Normalize provider traffic and limit units to decimal bytes inside the Decodo
adapter and retain the original provider unit/value only as sanitized audit
metadata. Reject profile creation or limit changes if the subscription response
does not make the unit safely interpretable.

### 9.4 Route and probe actions

- `POST /v1/proxy/routes` — validate and store a route spec; no paid request.
- `PUT /v1/proxy/routes/{id}` — narrow/change location, session, budget, label,
  or enabled state.
- `DELETE /v1/proxy/routes/{id}` — retire only when no active source policy
  references it.
- `POST /v1/proxy/routes/{id}/probe` — reserve a 1 MB application-transfer
  envelope, use a new session, and call only the fixed Decodo IP endpoint with a
  streaming client. Reject an oversized `Content-Length`, abort the stream when
  the application byte cap is reached, record/account the result, close the
  reservation, then reconcile. Reserve additional internal/provider safety
  margin because TLS and provider accounting can exceed response-body bytes;
  the full reservation and margin count against all normal budget gates. Limit
  probes per actor/profile and reject when any kill switch or budget gate is
  closed.

The probe response may show safe exit metadata, request latency, estimated
bytes, and reservation ID. It must not show a proxy URL or credentials.

### 9.5 Source-policy action

Extend `PUT /v1/sources/{id}` with an operator-only proxy block:

```json
{
  "proxy": {
    "policy": "never | fallback | always",
    "route_id": "uuid-or-null",
    "max_megabytes": 25,
    "pilot": true
  }
}
```

Rules:

- ordinary/manual run parameters can still only narrow policy;
- `always` is unavailable until three bounded successful pilot runs meet the
  existing evidence gate;
- `fallback` requires a classified eligible failure and an enabled route;
- the budget cannot exceed the route/profile/job/cycle limit;
- disabling a route/profile atomically changes dependent sources to `never`;
- source updates and provider/profile changes use row/advisory locks so the
  validation cannot race a disable or kill-switch action.

## 10. `/ops/proxies` interface

Add a **Proxies** tab after Metrics. Keep one page with progressive sections
rather than a separate mini-application.

### 10.1 Overview header

Show:

- provider connection and subscription validity;
- deployment switch (`disabled by deployment` is visually distinct from a DB
  kill switch);
- a 3.0 GB purchased gauge with a prominent 2.4 GB operational marker;
- provider-reported, application-estimated, active-reserved, and accounted
  totals;
- remaining cycle and dynamic daily allocations;
- reconciliation time/age and provider-vs-application discrepancy;
- pilot usage/300 MB and whether the pilot is active;
- active-cycle UTC boundaries and status, with admin-only **Propose next cycle**
  and **Open confirmed cycle** workflows that display every immutable allowance
  and never activate from a provider response alone;
- **Stop new paid traffic** as the most prominent action. Explain that existing
  leases drain unless the separate emergency revocation action is used.

Never use green solely because the provider API answered. Green requires a
fresh reconciliation, usage below limits, no kill switch, and an installed
profile. Stale provider data is warning/error and fails closed.

### 10.2 Usage

Provide day/hour series and tables for:

- total transmitted, received, and combined provider bytes;
- request count;
- application estimate versus provider total;
- top provider sub-users/profiles;
- top targets and countries when returned by Decodo;
- local usage by source, job, direct/browser proxy transport, and outcome;
- unexplained provider usage not attributable to local reservations.

Use decimal MB/GB everywhere. Include the provider's approximate 10–15 minute
statistics delay in the UI and do not label an estimate as billed traffic.

### 10.3 Reservations

Show active first, then recent closed/cancelled rows:

- source and links to job/run;
- profile/route and pilot marker;
- reserved versus estimated bytes and request count;
- state, age, and close time;
- warning for an active reservation whose job is terminal or lease is stale.

Provide a guarded **Cancel stale reservation** action that closes only a
provably terminal/orphaned reservation. It never subtracts already accounted
bytes.

### 10.4 Routes and “new proxy” wizard

The wizard fields are:

- logical profile;
- label;
- country and optional valid state/city;
- random or sticky session;
- duration, 1–1,440 minutes for sticky;
- HTTP, HTTPS, or SOCKS5 where the worker transport supports it;
- maximum bytes, default and maximum 25 MB without elevated approval;
- pilot flag.

After creation, show a redacted configuration preview and two separate buttons:

- **Save route** — no traffic;
- **Request/test session (1 MB application cap)** — paid probe with confirmation
  and a notice that the provider-accounted total may be slightly higher and the
  resulting exit may have been seen before.

For emergencies, expose **Revoke active paid leases** separately from the normal
kill switch. It activates the kill switch, marks active reservations
`revocation_requested`, and workers poll that flag between requests and while
streaming responses. Workers abort before the next request, close/account the
reservation monotonically, and record that already in-flight provider traffic
cannot be recalled. This action requires recent admin authentication and typed
confirmation; the default stop action remains the safer stop-new-leases control.

Do not offer a count of hundreds of generated endpoints. A worker creates a
fresh session identity per lease, which is the useful unit for this crawler.

### 10.5 Profiles / Decodo sub-users

Show safe fields only: label, masked username, provider ID, provider traffic
limit/use, auto-disable state, local enabled state, credential-generation age,
last metadata refresh, and referenced sources/routes.

The create form asks for label and traffic limit, not password. The server
generates and stores the password. Rotation does not reveal the new password.
Deletion is disabled while references or reservations exist.

### 10.6 Source eligibility and policy

Show candidate sources ranked by recent eligible direct failures, with:

- failure category and last occurrence;
- direct/impersonated/browser outcomes;
- prior proxy runs, records gained, failure-rate delta, bytes, and latency;
- current policy/route/budget;
- pilot evidence count and whether promotion criteria are met.

The UI must explain why a source is ineligible: parser failure, deterministic
404, robots decision, no classified access failure, or insufficient pilot
evidence.

### 10.7 Audit

Display sanitized operator and automated activity with actor, action, resource,
result, timestamp, and request ID. Make ambiguous provider mutations and local
credential-install failures impossible to miss.

## 11. Reconciliation and refresh jobs

Move provider refresh scheduling out of arbitrary worker replicas. Multiple
workers currently can notice the hourly interval; database locking prevents
bad accounting, but a single control-plane scheduler is clearer for provider
administration.

- control service reconciles every hour while deployment proxy support is on;
- consume the durable reconciliation-request outbox continuously; closing a
  proxied job/probe and completing a provider mutation enqueue there in the same
  database transaction, while a kill-switch-clear attempt enqueues and waits for
  a fresh result;
- reconcile promptly after those signals, coalescing bursts rather than relying
  on an in-memory callback, `NOTIFY`, or worker access to the Decodo API;
- use a PostgreSQL advisory lock so manual and scheduled reconciliations
  coalesce;
- fetch subscription, total traffic, daily traffic, proxy-user traffic, and
  targets with bounded pagination;
- persist normalized snapshots and update the cycle ledger in one transaction
  only after all required totals validate;
- partial detail failure may leave charts stale, but total-traffic failure sets
  `reconciliation_ok=false` and denies new leases;
- notify on reconciliation age, provider/application discrepancy, 50/80/90%
  operational use, daily capacity exhaustion, provider credential failure,
  and ambiguous mutations.

Provider statistics lag by roughly 10–15 minutes, so active reservations and
the internal margin remain part of the displayed accounted total. Workers no
longer mount `decodo.env` after control scheduling and the outbox path are live.

## 12. Observability

Add metrics without high-cardinality secrets:

- `catalogue_proxy_provider_reported_bytes{provider}`;
- `catalogue_proxy_application_bytes{provider}`;
- `catalogue_proxy_reserved_bytes{provider,state}`;
- `catalogue_proxy_reconciliation_age_seconds{provider}`;
- `catalogue_proxy_reconciliation_failures_total{provider}`;
- `catalogue_proxy_kill_switch{provider}`;
- `catalogue_proxy_probes_total{provider,result}`;
- `catalogue_proxy_provider_mutations_total{provider,action,result}`;
- `catalogue_proxy_budget_denials_total{provider,reason}`.

Do not label metrics by username, endpoint, session, target URL, job ID, or exit
IP. Those belong in bounded database reports, not Prometheus cardinality.

## 13. Testing

### 13.1 Provider adapter

- contract fixtures for subscription, traffic groupings, pagination, targets,
  sub-users, traffic limits, create/update/delete, and documented errors;
- malformed/oversized JSON, timeouts, redirects, 401/403, 429, and 5xx;
- safe-read retry and mutation non-retry behavior;
- API key/password/authenticated URL redaction from normal and exception logs;
- fixture drift test against the typed Decodo response models.

### 13.2 Database and concurrency

- migrations are additive and idempotent;
- snapshots are idempotent and totals never decrease;
- concurrent reconcile, reserve, kill-switch, profile disable, and source-policy
  changes serialize correctly;
- only one active provider cycle exists, opening/closing races are safe, and a
  worker cannot reserve against environment-only or expired boundaries;
- profile allocations plus external headroom never exceed the operational
  ceiling, including during blue-green overlap;
- stale reservation cancellation never reduces application bytes;
- audit rows are append-only and contain no known secret canaries;
- runtime database roles cannot alter audit history or use migration-owner
  privileges;
- source policy cannot reference disabled/retired route/profile metadata, and a
  queued job's resolved snapshot cannot be widened after creation;
- closing reservations creates durable, idempotent reconciliation requests and
  control recovers unprocessed requests after restart.

### 13.3 Secret lifecycle

- atomic file replacement and mode/owner checks;
- a worker sees a newly installed generation on its next job;
- interrupted writes leave the previous valid file active;
- provider-success/local-install-failure activates the kill switch;
- rotation never returns, logs, stores in DB, traces, or renders the password;
- in-place rotation is rejected with active reservations, drain-first rotation
  waits safely, and blue-green routing never assigns a new lease to the old
  generation after the cutover;
- containers without a declared need cannot read the secret volume.

### 13.4 API and authorization

- viewer versus admin permissions for every route;
- missing, forged, expired, replayed, wrong-audience, wrong-path, and wrong-method
  actor assertions are rejected by control even with a valid service token;
- origin/CSRF rejection and expired/recent-auth requirements;
- OpenAPI request/response validation and regenerated TypeScript drift checks;
- typed confirmations and idempotency keys;
- arbitrary URL, authenticated URL, invalid geo, over-budget, stale reconcile,
  and disabled deployment attempts are rejected;
- provider errors use RFC 9457 without response-body leakage.

### 13.5 UI

- decimal byte formatting and gauge thresholds;
- provider/application/reservation discrepancy states;
- deployment-disabled, kill-switched, stale, exhausted, and healthy states;
- route creation does not call the network;
- probe clearly distinguishes the 1 MB application cap from the larger charged
  safety envelope and never promises a unique exit;
- accessibility for gauges, tables, confirmations, disabled reasons, and mobile
  navigation;
- no secret appears in SSR HTML, hydration data, browser network responses, or
  client error messages.

### 13.6 Paid boundary

Use a local fake Decodo API and fake CONNECT/HTTP proxy in every automated
suite. Add a separate manually invoked live smoke command that:

- requires an explicit `--allow-paid-probe` flag;
- refuses more than 1 MB;
- streams and aborts an oversized response at the application cap while charging
  the full configured safety envelope to the ledger;
- uses only the fixed Decodo IP-check target;
- creates/resolves/closes a reservation and reconciles afterward;
- prints no credentials or endpoint URL.

## 14. Delivery order

Likely implementation files:

- `catalogue-dump/src/mb_ceramics_catalogue/providers/base.py` and
  `providers/decodo.py` for the typed provider boundary;
- `catalogue-dump/src/mb_ceramics_catalogue/proxy.py` for generalized
  job/probe reservations, safe route construction, and ledger controls;
- `catalogue-dump/src/mb_ceramics_catalogue/storage/schema/catalogue-ops-schema-v2.sql`
  for additive metadata, snapshots, probes, audit, events, and reservation
  ownership;
- `catalogue-control/src/catalogue_control/settings.py`, `queries.py`, and
  `app.py` for provider configuration, relational queries, routes, authority,
  and orchestration;
- `catalogue-control/catalogue-ops.openapi.json` followed by the existing
  OpenAPI/type generation commands;
- `catalogue-explorer/src/routes/ops/proxies/+page.server.ts` and
  `+page.svelte`, plus small components under `src/lib/ops/proxy/`;
- `catalogue-explorer/src/routes/ops/+layout.svelte` for the Proxies tab and
  proxy SSE topic;
- explorer server hooks/session modules for operator authorization and origin
  enforcement;
- `docker-compose.yml`, the control image entrypoint, and a one-shot secret
  volume initializer for private API-key staging and dynamic profiles, plus
  separate migration/control/worker/maintenance database credentials and the
  explorer-only actor-assertion private key/control verification-key set;
- provider/control/PostgreSQL tests in their existing suites, and explorer
  component/browser tests alongside the new route.

Keep provider adapter models in the shared Python package because workers and
maintenance commands already depend on it. Keep all browser-facing proxy types
generated from the control OpenAPI document; do not create a second handwritten
TypeScript contract.

### Phase 1: read-only manager

1. Add deployment-backed operator login, signed actor assertions, and
   control-side role enforcement; keep the page read-only until all three pass.
2. Add normalized provider models and the read-only Decodo adapter.
3. Add snapshot/profile/audit schema and migration ledger entry.
4. Mount `decodo.env` read-only in `catalogue-control` using the same private
   staging approach as workers.
5. Add overview, usage, reservation, profile, candidate, and audit GET routes.
6. Add `/ops/proxies` overview, usage, and reservations UI.
7. Deploy with proxy routing disabled and compare UI totals with the Decodo
   dashboard for at least 24 hours.

### Phase 2: ledger controls

1. Add scheduled/manual reconciliation with advisory locking.
2. Add the durable reconciliation-request outbox and switch after-job signaling
   to it before removing worker API-key mounts.
3. Add proposed/active/closed cycle workflows and change worker reservations to
   select the locked active database cycle.
4. Add kill-switch activation, guarded clearing, and separately confirmed active
   lease revocation.
5. Add pilot start/stop and stale-reservation cleanup.
6. Add notifications, metrics, audit events, and SSE refresh.
7. Split migration/control/worker/maintenance database roles and verify audit
   immutability under both runtime roles.
8. Verify failure of the control service/provider API cannot enable traffic.

### Phase 3: dynamic secrets and provider sub-users

1. Create and permission the `proxy-secrets` volume.
2. Seed it from rotated credentials, remove startup profile copying, and switch
   worker reads directly to the read-only volume.
3. Add allocation accounting and confirm provider limit/counting units against
   a live read response before enabling writes.
4. Add create, limit, drain-first/blue-green rotate, disable, refresh, and retire
   workflows.
5. Create one dedicated catalogue sub-user with provider-side auto-disable and
   a limit no greater than its currently unallocated operational share.
6. Verify provider mutation and local secret installation recovery procedures.

### Phase 4: routes and paid probe

1. Add route metadata and local endpoint construction.
2. Add the route wizard and redacted preview.
3. Add the fixed-target streaming probe with a 1 MB application cap, provider
   safety margin, reservation, and reconciliation.
4. Run one live smoke after explicit approval and compare estimated/provider
   traffic after the statistics delay.

### Phase 5: source policy and pilot

1. Add the relational source policy, immutable job snapshot, resolution
   precedence, eligibility query, and source policy control.
2. Keep `always` locked and start with one short HTTP and one browser candidate.
3. Run bounded 25 MB pilot jobs under the existing 300 MB total pilot cap.
4. Promote only sources satisfying three-run correctness, failure-rate,
   localization, latency, and sustainable-traffic criteria.
5. Leave all other sources on direct access.

## 15. Deployment and rollback

Before migration, back up PostgreSQL and the current external proxy-profile
file separately. Apply only additive tables/constraints and dual-read the old
static secret during the transition.

Deployment defaults:

- `CATALOGUE_PROXY_ENABLED=false`;
- database kill switch active;
- no source proxy policy changed;
- profile/sub-user mutations disabled until admin authentication and secret
  volume checks pass;
- paid probe feature disabled until read-only reconciliation has matched the
  dashboard for 24 hours.

Rollback order:

1. activate the database kill switch;
2. set `CATALOGUE_PROXY_ENABLED=false` and restart workers;
3. disable provider mutation routes;
4. restore the previous static secret mount if the dynamic volume is at fault;
5. deploy the previous control/explorer images;
6. retain additive metadata, snapshots, and audit rows for diagnosis.

Never roll back by lowering provider/application accounting or deleting audit
evidence.

## 16. Acceptance criteria

The proxy manager is complete when:

- an authenticated viewer can reconcile the 3 GB subscription, 2.4 GB
  operational ceiling, application usage, reservations, and discrepancy from
  `/ops/proxies` without seeing a secret;
- totals match the Decodo dashboard within its documented update delay and
  never decrease locally;
- **Stop new paid traffic** prevents every new lease under concurrency and is
  visible within one SSE refresh; separately confirmed revocation prevents each
  active lease from starting another request after observing the flag;
- a route can be created without traffic and a paid probe streams no more than
  1 MB of application data while reserving/accounting a documented provider
  safety envelope;
- an admin can create an allocated, auto-disabled sub-user and rotate it only
  after draining active leases or through a capacity-checked blue-green cutover,
  without a password appearing in the DB, UI, logs, traces, events, or API
  responses;
- a failed or ambiguous provider mutation produces an actionable audit state
  and fails closed;
- source proxy enablement is impossible without an enabled profile/route,
  current reconciliation, available budgets, and pilot eligibility;
- the worker resolves the authoritative active billing cycle from PostgreSQL and
  consumes only the immutable proxy snapshot recorded on its job;
- control rejects proxy administration unless both service authentication and a
  valid role-bearing actor assertion succeed;
- active reservations link to source/job/run details and stale reservations can
  be safely closed without reducing accounted usage;
- all fast, PostgreSQL, control API, explorer, OpenAPI/type-generation,
  redaction, concurrency, and fake-provider/proxy suites pass;
- the first live route remains globally disabled except for the explicitly
  authorized bounded probe/pilot.

## 17. Decodo references

- [Public API authentication and supported resources](https://help.decodo.com/api-reference/public-api-key-authentication)
- [Create a Residential sub-user](https://help.decodo.com/api-reference/sub-users/create-sub-user)
- [Update a sub-user password or traffic limit](https://help.decodo.com/api-reference/sub-users/update-sub-user)
- [Read per-sub-user traffic](https://help.decodo.com/api-reference/sub-users/get-sub-user-traffic)
- [Read subscription details](https://help.decodo.com/api-reference/subscriptions/get-subscriptions)
- [Read grouped traffic statistics](https://help.decodo.com/api-reference/traffic/get-traffic)
- [Residential backconnect setup and sticky sessions](https://help.decodo.com/docs/residential-proxy-quick-start)
- [Residential advanced parameters](https://help.decodo.com/docs/residential-proxy-advanced-parameters)
- [How Decodo counts proxy traffic](https://help.decodo.com/docs/proxy-traffic)
