# Reusable scraping architecture migration plan

Status: code migration implemented behind explicit canaries; production shadow,
allowlist rollout, deployment-capacity evidence, and legacy-path retirement
remain
Prepared: 2026-08-15
Scope: `catalogue-dump`, catalogue workers, control-plane source configuration,
artifacts, PostgreSQL loading, monitoring, and platform scraper tests.

Implementation snapshot (2026-08-15):

- phase 1 contracts, dataset registry, ceramics compatibility/identity
  projectors, bounded batch store, priority budget, and platform-neutral runner
  are implemented;
- phase 1.5 Camoufox/CDP browser abstraction, authenticated disposable
  `cdp_extension_proxy` leasing, exact-backend worker routing, and readiness
  gating are implemented;
- additive PostgreSQL page/checkpoint/dataset/artifact state, degraded job state,
  dataset-aware control/retention/promotion/download behavior, and deterministic
  ordered resume/publication are implemented;
- Shopify, WooCommerce, BigCommerce, Wix, PrestaShop, and SIO-2 have neutral
  connectors and explicit compatibility canaries while their legacy registry
  keys remain the default; Shopify optional exact-inventory enrichment is
  budget-aware and preserves discovery capacity;
- price, stock, and document observation datasets can fan out from the same
  bounded connector page;
- the generic PageCommerce connector now covers bounded sitemap/category
  discovery plus JSON-LD, microdata, OpenGraph, and reviewed DOM selectors;
- every scraper family referenced by the checked-in source configuration has a
  neutral runtime, a distinct canary selector, and its unchanged legacy rollback
  selector; a strict registry matrix prevents partial registrations;
- shared priority budgets are wired through every registered runtime; required
  exhaustion is resumable and incomplete, while optional work preserves future
  discovery capacity and the external proxy lease remains the spending authority;
- `catalogue-shadow-compare` provides bounded, artifact-only legacy/canary
  comparison without a second crawl, and scaling tests prove bounded retained
  normalized entities across 2,000 streamed pages;
- production shadow/allowlist rollout, deployment-capacity evidence, and final
  legacy removal remain operator actions. That evidence cannot be manufactured
  by unit tests.

## 1. Outcome

Turn the current ceramics-specific scraper collection into a reusable data
collection platform without rewriting the working transport, proxy, scheduling,
or platform integrations.

The migration is complete when:

- Shopify, WooCommerce, Wix, BigCommerce, PrestaShop, and the generic page
  crawler extract into typed, platform-neutral commerce snapshots whose
  time-varying facts carry their own observation time and evidence;
- ceramic classification and enrichment consume neutral snapshots through a
  dataset projector instead of being called directly by platform scrapers;
- one platform crawl can produce catalogue items, manufacturer identity
  records, price observations, stock observations, document links, and future
  datasets without repeating discovery or fetching;
- adding a retail vertical requires a projector and enrichment modules, not a
  copy of each platform scraper;
- adding a platform requires a connector and mapping tests, not changes to the
  ceramics schema or runner;
- pagination, essential product data, and optional enrichment have declared
  priorities and independent budgets;
- large crawls commit page output and checkpoints atomically, then resume without
  losing or duplicating already committed records;
- peak pipeline memory is bounded by page size and enabled projectors rather than
  total source size;
- browser-dependent connectors can run through either the existing managed
  Camoufox backend or the operator-managed `projects-caddy/cdp-extension-proxy`
  Chromium backend without changing connector or projector code;
- every migrated source is output-compatible with its pre-migration golden
  artifact unless an intentional contract migration is reviewed separately;
- existing schedules, control APIs, proxy policies, loaders, and explorer
  queries remain operational throughout the migration.

## 2. Non-goals

This is not:

- a rewrite of the worker queue, control service, proxy manager, or storage
  history model;
- a universal crawler framework for arbitrary websites;
- permission to infer fields that a source did not publish;
- a change to robots, rate-limit, proxy, or source-retirement safety policy;
- a migration to event streaming, Redis, Kafka, or another queue;
- a reason to combine all platform behavior into one configurable scraper;
- a schema-breaking replacement of `ceramics.catalogue_item.v2` in the first
  phases.

The first objective is separation of responsibilities and code reuse. New
infrastructure is added only where that separation requires it.

## 3. Current architecture assessment

### 3.1 Reusable foundation to keep

The following code is already broadly reusable and should move only when a
specific interface improvement requires it:

- `crawl/session.py`: client, cache, browser, limiter, and proxy lifecycle;
- `scrapers/base.py::Fetcher`: HTTP, browser, impersonation, caching, conditional
  requests, adaptive rate limits, proxy fallback, traffic accounting, and
  direct-route circuit breaking;
- `scrapers/base.py::HostLimiter`: per-host and shared-edge pacing;
- `projects-caddy/cdp-extension-proxy`: an optional Chromium CDP endpoint reached
  through its MV3 extension and Native Messaging host, retained as an external,
  operator-managed browser service rather than copied into this package;
- `crawl/runner.py`: deadlines, cancellation, progress, artifact output, and
  per-source execution;
- `ops`: leases, jobs, schedules, reservations, monitoring, and recording;
- `providers`: provider-neutral proxy operations and Decodo integration;
- `scrapers/enrichment.py`: named, dependency-aware enrichment modules;
- storage history, source retirement protection, and canonical promotion;
- source validation with `extra="forbid"`.

These parts solve operational problems independent of ceramics and should be
treated as stable services offered to connectors and pipelines.

### 3.2 Coupling to remove

The principal coupling points are:

1. Platform scrapers call `scrapers.record.build()` directly. Transport parsing,
   commerce normalization, ceramics enrichment, validity, and scope filtering
   therefore happen in one call path.
2. `ScrapeResult.records` is `list[dict[str, Any]]`, so incompatible record
   shapes cannot be checked before storage.
3. `SourceConfig` contains common policy, platform options, and ceramics scope
   options in one model, then projects back to an untyped dictionary.
4. `crawl/runner.py` names scraper implementations when deciding whether a
   price-only refresh is supported.
5. Connector capabilities such as exact stock, incremental cursors, browser
   requirements, and document extraction are implicit in implementation and
   source flags.
6. Discovery and optional product enrichment share one request budget. The
   Ulster failure demonstrated why pagination must have higher priority.
7. Platform-specific tests often assert final ceramics rows, making it hard to
   tell whether a regression occurred in extraction, normalization, enrichment,
   or projection.
8. Checkpoints are artifacts of a completed job rather than first-class cursor
   state, so a large source normally restarts after interruption.

### 3.3 Existing design patterns to extend

The migration should extend patterns already proven in this repository:

- use typed Pydantic models at external/configuration boundaries;
- use small protocols for provider-neutral interfaces;
- register explicit modules and reject unknown names;
- use immutable or context-bound run configuration;
- preserve partial results while refusing destructive retirement when a
  catalogue is incomplete;
- make optional inference opt-in and record its provenance;
- use recorded responses and golden artifacts rather than live websites in CI.

### 3.4 Transport compatibility ladder

Do not describe these modes as generic security bypasses. They are a bounded
compatibility ladder for public, authorized collection, used only when the
lower-cost mode cannot correctly read a source. Robots policy, rate limits,
operator proxy budgets, authentication boundaries, and source-specific approval
still apply at every layer.

There are five supported execution modes and one deliberately restricted
diagnostic mechanism:

| Mode | Implementation | Intended use | Production policy |
| --- | --- | --- | --- |
| Plain HTTP | `httpx` through `Fetcher` | APIs, feeds, HTML, sitemaps | default |
| TLS/browser impersonation | optional `curl_cffi` client | public hosts that reject non-browser TLS handshakes | retry only after an ordinary refusal |
| Managed Firefox-compatible browser | Camoufox `BrowserBackend` | JavaScript rendering and browser-origin requests | tested backend, job-isolated pages/state |
| Browser-session request/evaluation | backend-neutral `BrowserSession` | public data available only from the page's own runtime | connector sees only the protocol, not backend APIs |
| Managed Chromium CDP service | Playwright attached to `cdp_extension_proxy` | sources explicitly tested against Chromium or extension-mediated networking | authenticated, direct-only, attested, capacity-one disposable lease |
| Raw CDP commands | operator diagnostics only | backend bring-up and narrowly reviewed missing protocol features | never imported or issued by connectors; promote a needed operation into `BrowserSession` after contract tests |

Playwright is the safe CDP client used by the managed Chromium mode, not another
automatic escalation layer and not a locally launched second Chromium. Likewise,
`cdp_extension_proxy` is a deployment/backend choice, not permission to route a
source through a proxy: production readiness rejects its proxy-routed,
unauthenticated, or persistent-shared-profile configurations.

The ladder never includes CAPTCHA solving, credential harvesting, stolen browser
state, cross-job cookie reuse, challenge-token resale, or attempts to defeat an
origin that has explicitly denied collection. A connector records a typed
blocked/incomplete outcome when approved modes are exhausted.

## 4. Target architecture

```text
source policy + run parameters
             │
             ▼
      CrawlSession / Fetcher
             │
             ▼
        PlatformConnector
  discover → fetch → parse → normalize
             │
             ▼
 CommerceProductSnapshot stream
product with variants / offers / stock / documents
             │
       ┌─────┴────────────┐
       ▼                  ▼
 DatasetProjector   ObservationProjector
 ceramics items    price / stock / documents
       │                  │
       └─────┬────────────┘
             ▼
 validation → artifact → loader → promotion
```

The boundaries are:

- a **connector** understands a remote platform and only facts that platform
  published;
- a **normalizer** converts platform payloads to neutral commerce entities;
- a **projector** chooses a dataset contract and maps neutral facts into it;
- an **enricher** derives domain facts and records how they were inferred;
- a **sink** validates and persists dataset records;
- the **runner** coordinates these components without naming individual
  platforms or datasets.

## 5. Package layout

Introduce the following layout incrementally:

```text
mb_ceramics_catalogue/
  transports/                # eventual home of generic fetch infrastructure
    protocols.py
    priorities.py
    browser.py               # backend-neutral render/evaluate/in-page request
    camoufox.py               # managed local Firefox-compatible backend
    cdp_extension_proxy.py   # attach to operator-managed Chromium CDP service
  connectors/
    base.py                  # connector protocols and capabilities
    commerce.py              # neutral commerce entities
    shopify.py
    woocommerce.py
    wix.py
    ...
  datasets/
    base.py                  # projector and contract protocols
    registry.py
    ceramics/
      contract.py
      projector.py
      enrichments.py
      scope.py
    commerce/
      price_observation.py
      stock_observation.py
      document.py
  pipeline/
    runner.py                # entity fan-out and dataset projection
    checkpoints.py
    outputs.py               # per-dataset state and durable page batches
    validation.py
```

Do not move `Fetcher` or existing scraper files at the beginning. First add the
new interfaces beside them. Move generic transport code only after connectors
use the new interface, otherwise file movement obscures behavioral changes.

During migration, `scrapers/` remains a compatibility package. Its registry can
return an adapter that wraps a new connector and projector while untouched
scrapers continue using the current `Scraper` contract.

## 6. Neutral commerce contracts

### 6.1 Principles

Neutral snapshots contain published source facts, not ceramic interpretation.
They must:

- distinguish a product from a purchasable variant;
- distinguish an offer from product identity;
- represent missing, unknown, and explicitly unavailable values separately;
- carry source timestamps and stable external identifiers;
- retain platform extensions without promoting unreviewed fields into the
  common contract;
- attach evidence to sensitive facts such as stock quantity;
- avoid floats for money;
- remain serializable for replay tests and intermediate artifacts.

The initial common contract is deliberately an aggregate
`CommerceProductSnapshot`, not a mixed union of product, variant, offer, stock,
and document events. A snapshot represents the platform's published view of one
product at one observation time. Independent dataset records are produced by
projectors. If a future platform genuinely supplies independent event streams,
that requires a reviewed contract version rather than an ambiguous widening of
this one.

### 6.2 Core types

Add immutable typed models similar to:

```python
class Money(BaseModel):
    amount: Decimal
    currency: str


class Evidence(BaseModel):
    method: Literal["api", "jsonld", "html", "cart_ceiling", "browser"]
    source_url: str
    source_field: str | None = None
    observed_at: datetime
    confidence: Literal["published", "verified", "derived"] = "published"


class CommerceVariant(BaseModel):
    external_id: str
    title: str | None = None
    sku: str | None = None
    gtin: str | None = None
    options: dict[str, str] = Field(default_factory=dict)
    offers: list[CommerceOffer] = Field(default_factory=list)
    stock: StockState | None = None
    published_attributes: dict[str, JsonValue] = Field(default_factory=dict)


class CommerceProductSnapshot(BaseModel):
    source_id: str
    external_id: str
    canonical_url: str
    title: str
    description: str | None = None
    vendor: str | None = None
    observed_at: datetime
    categories: list[CategoryRef] = Field(default_factory=list)
    images: list[MediaRef] = Field(default_factory=list)
    documents: list[DocumentRef] = Field(default_factory=list)
    variants: list[CommerceVariant] = Field(default_factory=list)
    source_updated_at: datetime | None = None
    platform_extensions: dict[str, JsonValue] = Field(default_factory=dict)
```

Product and variant external identifiers are unique only within their declared
source and connector namespace; global keys include that namespace. Snapshot
and evidence timestamps are supplied explicitly, never read from the wall clock
inside a projector. Tests freeze them, making projection deterministic.

`CommerceOffer` must support more than one offer per variant and preserve:

- amount and currency;
- the time this offer was observed and its evidence;
- offer role such as regular, sale, member, or quantity tier;
- VAT status and rate when published or source-configured;
- minimum quantity, unit, pack size, and price-validity interval;
- seller identity when the platform is a marketplace;
- availability and its evidence.

Projectors must not guess how several offers collapse into one catalogue price.
That selection is an explicit, versioned dataset rule.

Every time-varying nested fact is itself an observation envelope. In
particular, `CommerceOffer`, `StockState`, and `DocumentRef` carry their own
`observed_at` and evidence. A connector omits a fact it did not observe in the
current collection; it must never copy a previous run's value into a new
snapshot. Dataset projectors emit an observation only from a nested fact that
was observed in this collection. This preserves one convenient bounded product
aggregate without representing stale price or stock as fresh.

Manufacturer catalogues may publish identity and specifications without a
purchasable variant or offer. An empty `variants` list is valid and the snapshot
preserves manufacturer references and specifications for projection to
`ceramics.catalogue_identity.v2`.

### 6.3 Availability and stock

Stock is particularly easy to overstate. Model it explicitly:

```python
class StockState(BaseModel):
    availability: Literal[
        "in_stock", "out_of_stock", "backorder", "preorder", "unknown"
    ]
    quantity: int | None = Field(default=None, ge=0)
    quantity_kind: Literal[
        "exact", "lower_bound", "upper_bound", "order_limit", "unknown"
    ] = "unknown"
    observed_at: datetime
    evidence: list[Evidence]
```

Only `quantity_kind="exact"` may populate the current catalogue's
`stock_quantity`. Cart limits and order-policy ceilings remain separate facts.

### 6.4 Collection pages and cursors

Connectors return pages rather than appending directly to `ScrapeResult`:

```python
class EntityPage(BaseModel, Generic[T]):
    page_id: str
    sequence: int = Field(ge=0)
    items: list[T]
    resume_after: JsonValue | None = None
    terminal: bool
    partition_terminal: bool = False
    enumeration_intact: bool = True
    discovered: int
    diagnostics: list[Diagnostic] = Field(default_factory=list)
```

`page_id` is stable for the same connector version, source, configuration, and
logical page. `sequence` is monotonic within a sequential lineage and supplies
deterministic compaction order after a restart; partitioned collection uses the
pair `(partition_key, sequence)`. `resume_after` unambiguously means the cursor to use only after
this page's outputs have been durably committed; it is not the cursor used to
fetch the page. Both are connector-owned but JSON-serializable.

`terminal`, `partition_terminal`, and `enumeration_intact` are deliberately
independent. `terminal` means the whole collection stream ended;
`partition_terminal` marks the last committed page of one declared partition.
Every ordinary non-final page has both terminal flags false and
`enumeration_intact=True`. An intermediate partition terminal has
`terminal=False, partition_terminal=True` and carries the connector-owned cursor
for the next partition. The final successful page has `terminal=True` and
`enumeration_intact=True`; collection terminality also implies partition
terminality when persisted. A connector that cannot continue emits a terminal page or
the runner records an equivalent terminal outcome when the connector raises,
with `enumeration_intact=False` and a diagnostic. Enumeration integrity is
sticky within a lineage: once false, no later page can restore it.
`resume_after` must be `None` for a successful collection-terminal page, while
an intermediate partition terminal must carry the exact next-partition cursor.
Reaching a caller-supplied result limit is an incomplete terminal outcome with a
typed diagnostic and an exact resume cursor; it is never natural exhaustion.
Retirement is permitted only after a committed terminal outcome with
`enumeration_intact=True`; no intermediate page makes a retirement decision.
`enumeration_intact=False` has the same safety meaning as today's
`truncated=True`.

## 7. Connector contract

### 7.1 Interface

Define a narrow protocol:

```python
class CommerceConnector(Protocol):
    name: str
    platform: str
    capabilities: ConnectorCapabilities

    async def collect(
        self,
        request: CollectionRequest,
        checkpoint: ConnectorCheckpoint | None = None,
    ) -> AsyncIterator[EntityPage[CommerceProductSnapshot]]: ...
```

`CollectionRequest` contains dataset-independent collection intent:

- source identity and base URL;
- full or incremental mode;
- requested capability fields;
- result limit;
- category/collection discovery restrictions;
- request-priority budget;
- cancellation/checkpoint callback.

The connector receives `Fetcher` or a smaller `Transport` protocol. It does not
receive a ceramics projector or database connection.

### 7.2 Capabilities

Capabilities replace implementation-name checks:

```python
class ConnectorCapabilities(BaseModel):
    snapshot_fields: frozenset[SnapshotField]
    refresh_modes: frozenset[RefreshMode]
    stock_kinds: frozenset[StockQuantityKind]
    supports_incremental_cursor: bool
    supports_category_filter: bool
    supports_documents: bool
    browser: Literal["never", "optional", "required"]
    browser_backends: frozenset[Literal["camoufox", "cdp_extension_proxy"]]
    shared_edge: str | None
```

The runner asks whether the requested dataset and refresh mode are supported.
It never checks `config.scraper in {...}`.

`browser_backends` declares tested compatibility, not a preference or a CDP
endpoint. A browser-dependent job puts the generic `browser` capability in its
existing all-of `requires` column. An explicitly selected backend also goes in
`requires`. For `auto`, add an additive `requires_any text[]` job column
containing the connector/source intersection, such as
`{"browser:camoufox", "browser:cdp_extension_proxy"}`. Claiming requires:

```sql
j.requires <@ worker_capabilities
and (
  cardinality(j.requires_any) = 0
  or j.requires_any && worker_capabilities
)
```

Workers advertise `browser` only when at least one probed backend is ready, plus
each ready exact capability. When an `auto` job starts, the worker selects one
capability from the intersection and atomically records it in a non-secret
`selected_browser_backend` job field before collection. Checkpoints, summaries,
diagnostics, and retries use that selection. A retry may change it only by
explicitly rejecting the old checkpoint lineage and starting a new collection.
Runtime discovery may add the generic and allowed-any requirements as today, but
it must not silently change to an unapproved backend.

### 7.3 Diagnostics

Replace ad hoc error strings at the connector boundary with typed diagnostics:

- `enumeration_incomplete`;
- `entity_fetch_failed`;
- `parser_unsupported`;
- `rate_limited`;
- `proxy_budget_exhausted`;
- `optional_enrichment_skipped`;
- `schema_changed`;
- `checkpoint_invalid`.

Each diagnostic declares severity, affected URL/entity, retryability, whether
catalogue completeness is affected, and a safe operator message. Existing
summary error strings can be produced by the compatibility adapter.

### 7.4 Browser transport backends

Connectors use a narrow job-scoped `BrowserSession` protocol exposed by
`Fetcher`; they
do not import Camoufox, Playwright, Chromium CDP types, or the extension proxy:

```python
class BrowserBackend(Protocol):
    backend: Literal["camoufox", "cdp_extension_proxy"]

    async def open_session(self, job: BrowserJobContext) \
            -> AsyncContextManager[BrowserSession]: ...
    async def shutdown(self) -> None: ...


class BrowserSession(Protocol):
    async def render(self, url: str, wait: BrowserWait) -> RenderedDocument: ...
    async def evaluate(self, url: str, script: ReviewedScript,
                       wait: BrowserWait) -> JsonValue: ...
    async def request_json(self, page_url: str, endpoint: str,
                           request: BrowserRequest) -> JsonValue: ...
    async def close(self) -> None: ...
```

`BrowserBackend` is process-owned; `BrowserSession` is job-owned. Closing a
Camoufox job session closes only that job's pages and state, while worker
shutdown closes the shared browser process. Closing a CDP job session closes its
attributed target, disconnects its client, releases its endpoint lease, and
requests destruction of its disposable browser instance; it never shuts down an
unrelated operator browser.

The Camoufox adapter preserves the current managed-browser behavior. The
`cdp_extension_proxy` adapter attaches Playwright Chromium through the proxy's
browser-level CDP endpoint, obtains or creates a page target, and implements the
same three operations. The external service owns Chromium, its extension, and
Native Messaging lifecycle; the catalogue worker never starts it with remote-
debugging flags and never assumes full CDP compatibility beyond the proxy's
documented command subset.

Each CDP-backed job receives a disposable Chromium user-data directory or a true
isolated browser context, verifies that every returned target belongs to that
isolation boundary, and destroys it in `finally` paths. A fresh tab in the
proxy's current persistent default context is explicitly insufficient because
it shares cookies, storage, cache, and service workers across sequential jobs.
Until `cdp-extension-proxy` supports contract-tested isolated contexts, its
production pool provisions one clean proxy/Chromium instance and profile per
job, capacity one, then destroys and replaces the instance after release. The
current persistent-profile Docker mode is development/diagnostic only.

Provisioning is represented by an operator-owned `CdpEndpointProvider`, not
shell commands inside connectors. `acquire(job_id, logical_profile)` returns a
short-lived endpoint lease containing an opaque instance id, secret-resolved
connection information, attested route/isolation/service generation, and
expiry. `release(lease, destroy=True)` destroys the Chromium process and
user-data directory even when target cleanup failed. A static endpoint provider
is permitted only for development diagnostics. The initial production provider
may manage a bounded pre-warmed container pool, but an instance is never handed
to another job until its previous profile has been destroyed and a new clean
generation has been attested.

Because the current proxy MVP has no multi-client attach policy, every endpoint
is protected by a distributed capacity-one lease unless a later proxy release
advertises and tests a higher safe capacity. Losing the WebSocket is a typed,
retryable browser-backend failure, never evidence that enumeration completed.

The first production CDP integration is direct-route only. Its logical profile
declares `route="direct"`, the Chromium service starts with `PROXY_SERVER=`, and
worker readiness verifies operator-provided route/isolation attestation. A CDP
endpoint using an external or paid proxy is rejected. CDP proxy routing remains
disabled until the service can bind a job to a PostgreSQL proxy reservation,
enforce its route and byte ceiling, attribute navigation and subresource bytes,
fail closed on expiry, and reconcile usage with the provider ledger. Browser
traffic must never bypass `ProxyLease` spending authority merely because it runs
outside `Fetcher`.

Browser scripts and destination URLs remain connector-owned reviewed code. Run
requests cannot submit arbitrary CDP methods, JavaScript, target identifiers, or
navigation URLs. This integration is a compatibility backend for permitted
public collection, not a challenge, CAPTCHA, authentication, or access-control
bypass mechanism.

## 8. Dataset and projector contracts

### 8.1 Dataset registry

Add a registry parallel to the connector registry:

```python
class DatasetDefinition(Protocol):
    name: str
    version: str
    record_model: type[BaseModel]
    required_snapshot_fields: frozenset[SnapshotField]
    required_capabilities: frozenset[str]

    def project(self, entity: CommerceProductSnapshot, context: ProjectionContext) \
            -> Iterable[DatasetRecord]: ...
```

Initial registrations:

- `ceramics.catalogue_item.v2`;
- `ceramics.catalogue_identity.v2`;
- `commerce.price_observation.v1`;
- `commerce.stock_observation.v1`;
- `commerce.document.v1`.

`record_model` is the concrete versioned Pydantic contract for that dataset;
`DatasetRecord` is only protocol shorthand. Every projected record is validated
against it before staging, and its JSON schema and canonical serialization are
frozen in contract tests. The compatibility adapter converts validated ceramics
records to legacy dictionaries only at the old boundary.

### 8.2 Ceramics compatibility projector

Move the behavior currently reached through `record.build()` into a ceramics
projector in stages:

1. map neutral identity, offer, stock, media, category, and attribute fields;
2. invoke the selected ceramics enrichment modules;
3. apply ceramics scope and exclusions;
4. call the existing record validation and stable-ID functions;
5. emit exactly `ceramics.catalogue_item.v2` or, for the explicitly selected
   identity dataset, `ceramics.catalogue_identity.v2`.

Initially the projector may call `record.build()` internally. Once all major
connectors use it and parity is proven, split `record.py` into contract,
projection, enrichment, and validation modules.

### 8.3 Multiple outputs from one crawl

The pipeline should fetch once and project many times:

```text
CommerceProductSnapshot
  ├── ceramics catalogue rows
  ├── ceramics manufacturer identity rows
  ├── price observations
  ├── exact-stock observations
  └── document records
```

Projection must be pure and deterministic. A projector may not perform remote
requests. If a dataset needs another remote field, it declares a capability in
the collection request before the connector starts.

### 8.4 Field ownership

Define field ownership before enabling partial refreshes:

| Field group | Owner | Typical refresh |
| --- | --- | --- |
| identity, URL, variant options | connector identity | daily/full |
| price, currency, availability | offer projector | daily |
| exact stock | stock projector | configured cadence |
| descriptions, images, documents | detail collection | weekly/change-based |
| ceramics firing/form/surface | ceramics enrichment | when evidence changes |

A partial dataset update may advance fields it owns and must not erase fields
owned by an unexecuted projector.

### 8.5 Collection intents and cadence

Every scheduled or manual run selects an explicit set of datasets. Before
fetching, the pipeline validates them against connector capabilities and unions
their required snapshot fields into one immutable collection request. That is
the unit of fetch reuse: datasets selected in the same collection share remote
work; a stock-only run at a different cadence does not pretend to reuse a crawl
that has already ended.

Schedules may therefore select different dataset sets, for example a daily
price run and a weekly full catalogue-and-document run. Job and dataset state
record the selected intent. A lightweight request cannot promote or retire
fields owned by datasets it did not execute.

## 9. Typed source configuration

### 9.1 Separate concerns

Replace the growing flat model with three validated sections while retaining a
loader for the current JSON shape:

```json
{
  "source": {"label": "...", "url": "...", "country": "GB"},
  "crawl": {
    "delay": 0,
    "timeout_seconds": 3600,
    "browser": {"backend": "auto"}
  },
  "connector": {
    "type": "shopify",
    "options": {"collections": [], "inventory": {"method": "html"}}
  },
  "datasets": {
    "ceramics.catalogue_item.v2": {
      "scope": "materials",
      "enrichments": ["ceramic-materials"]
    },
    "commerce.stock_observation.v1": {"enabled": true}
  }
}
```

### 9.2 Discriminated platform options

Use a discriminated union for connector configuration:

- `ShopifyOptions`;
- `WooCommerceOptions`;
- `WixOptions`;
- `PageCrawlOptions`;
- one model per remaining connector.

This prevents Shopify inventory flags from appearing valid on WooCommerce and
eliminates silent `dict.get()` defaults over time.

### 9.3 Compatibility

Do not rewrite `sources.json` in phase 1. Add:

- a legacy-to-typed projection with golden tests for every current source;
- serialization of the projected form for inspection;
- a command that reports ambiguous or unused legacy fields;
- a later mechanical migration after all active fields have typed owners.

The control-plane database overlay must use the same typed models. Checked-in
configuration and operator overrides may not acquire different semantics.

Browser source configuration may select only `auto`, `camoufox`, or
`cdp_extension_proxy`, plus an optional logical operator profile name. It may
not contain a CDP URL, token, extension id, browser profile path, or arbitrary
Chromium arguments. Logical CDP profiles are resolved from worker-owned secret
configuration and snapshot only non-secret profile identity and backend
capability onto a job.

An operator CDP profile defines the internal endpoint, token secret reference,
health timeout, capacity, allowed worker pool, route mode, isolation mode, and
expected service/profile generation. Production profiles require token
authentication, a loopback or private-network endpoint, `route="direct"`, and
either an ephemeral instance/profile per job or contract-tested isolated browser
contexts. The current `cdp-extension-proxy` Docker defaults—unauthenticated,
proxy-routed, and backed by one persistent Chromium profile—are development-only
and rejected by production readiness validation.

The cross-project service contract must expose or provide trusted deployment
attestation for instance identity, service version, route mode, isolation mode,
and clean profile generation. A worker advertises
`browser:cdp_extension_proxy` only after these values match its logical profile
and the required CDP command probe succeeds.

## 10. Priority-aware request budgeting

### 10.1 Priority classes

Generalize the Shopify feed reserve into transport priorities:

1. `DISCOVERY`: robots, sitemap, catalogue/category pagination;
2. `IDENTITY`: product identity required for a valid base record;
3. `DATASET_REQUIRED`: remote facts required by at least one requested dataset;
4. `DETAIL`: facts requested for completeness but allowed to degrade under the
   selected dataset's declared policy;
5. `OPTIONAL`: additional expensive enrichment not required by any selected
   dataset.

The ordering expresses data completeness, not request urgency. Discovery is
always protected. The importance of offer, stock, document, or technical-detail
work is derived from the selected dataset definitions rather than hard-coded by
field name. Exact stock is optional during an ordinary ceramics catalogue run,
but `DATASET_REQUIRED` when `commerce.stock_observation.v1` requests exact
quantities. Optional work may use remaining capacity but may never consume a
reservation needed for discovery or another dataset's required fields.

### 10.2 Budget object

Introduce a per-job `RequestBudget` with:

- direct request/time allowance;
- browser request/time allowance;
- paid-proxy bytes reserved and used;
- per-priority reserves and ceilings;
- estimates updated from observed response sizes;
- the datasets that require each planned request and their degradation policy;
- a decision method returning allow, downgrade, defer, or deny.

The first implementation wraps existing counters and proxy leases. It must not
create a second spending authority: the PostgreSQL/Decodo reservation remains
the hard limit.

### 10.3 Planning optional work

After each page, estimate optional detail cost from observed mean and a
conservative high percentile. Stop optional work before protected discovery and
required-dataset reserves. Skipped optional work is a note/metric. Skipped
required work marks only the affected dataset output partial, degraded, or
failed according to its declared completeness policy; it may not be hidden as a
green note, and it does not invalidate an otherwise complete catalogue output.

Static constants such as the current 1 MB Shopify reserve remain as a safe
fallback until enough measurements exist. Learned estimates may reduce work,
never override the hard reservation.

## 11. Checkpointing and resumability

Checkpointing is inseparable from durable page output. Advancing a cursor while
projected rows exist only in process memory loses those rows after a crash;
persisting rows before a separately committed cursor duplicates them when the
page is retried. The pipeline therefore commits a page manifest and its next
cursor as one logical operation.

### 11.1 Checkpoint contents

A checkpoint contains:

- source, connector, and connector version;
- normalized configuration fingerprint;
- current enumeration cursor;
- last committed `page_id` and committed page identifiers required for
  idempotency;
- selected datasets and their contract versions;
- observed budget state;
- creation time and expiry;
- checksum.

Never store proxy credentials, cookies, authenticated URLs, or browser storage
in a checkpoint.

### 11.2 Persistence

For each connector page, the pipeline:

1. validates the stable `page_id` and projects the page into bounded dataset
   batches;
2. writes each batch to an attempt-scoped staging object using a deterministic
   key and computes its checksum;
3. in one PostgreSQL transaction, inserts the page manifest and dataset batch
   metadata, records projector outcomes, and advances the checkpoint to the
   page's `resume_after` cursor;
4. treats a repeated commit of the same `(job, checkpoint lineage, page_id,
   dataset, projector version)` and checksum as success;
5. rejects the same identity with a different checksum as nondeterminism or
   checkpoint corruption.

The transaction never claims that a staging object exists until its checksum is
readable. Orphaned attempt-scoped objects are harmless and removed by bounded
retention. Completed dataset artifacts are compacted from committed page
batches; process memory and cancelled-job partial artifacts are not resume
state.

The small current checkpoint and page manifests live in PostgreSQL and their
identifiers appear in job events. Completed immutable artifacts remain the
durable audit representation.

### 11.3 Resume rules

Resume only when connector version, source URL, relevant configuration
fingerprint, and dataset contract are compatible. Otherwise start from the
beginning and retain the rejected checkpoint for diagnosis.

A resumed full crawl is not complete until it reaches the connector's terminal
cursor. Retirement remains withheld for cancelled, expired, or incomplete
checkpoint chains.

A resume reconstructs output from every committed page in the lineage, not only
pages fetched by the latest worker attempt. A terminal checkpoint may be marked
complete only after all expected page or partition manifests are present. Final
artifact publication and dataset promotion are idempotent and may safely be
retried after a crash.

## 12. Storage and artifact evolution

### 12.1 Artifact envelope

Add an envelope that identifies:

- dataset name and version;
- connector and version;
- source and run;
- projection configuration fingerprint;
- whether enumeration completed;
- checkpoint lineage;
- record count and checksum.

Continue producing the current artifact shape for the ceramics compatibility
path until every reader understands the envelope.

### 12.2 Intermediate neutral artifacts

Do not enable permanent neutral-snapshot storage by default. It can multiply
storage and duplicate source payloads. Initially:

- keep neutral snapshots in bounded page streams during a healthy attempt;
- durably stage projected dataset batches and page manifests for crash-safe
  resume, as specified in section 11, without retaining source payloads;
- permit an opt-in compressed diagnostic artifact for replay and migration;
- retain sanitized recorded HTTP fixtures for connector tests;
- evaluate durable neutral artifacts only if cross-dataset reprocessing proves
  cheaper than refetching and the retention cost is measured.

### 12.3 Dataset-specific sinks

Keep the existing ceramics loader behind a `DatasetSink` adapter. Add new
tables/loaders only for approved contracts. Each sink declares idempotency,
partial-update behavior, retirement semantics, and history retention.

Stock observations should record only a change or advance `last_seen_at`, using
the same history principle as offer observations; they should not create an
unchanged row every crawl.

### 12.4 Multi-dataset job state

The existing one-job/one-artifact columns remain the ceramics compatibility
view, but cannot be the new source of truth. Add additive operational tables
equivalent to:

```text
job_datasets(job_id, dataset, contract_version, projector_version,
             state, complete, records, rejected, error, promoted_at)
job_pages(job_id, checkpoint_lineage, partition_key, page_id, page_sequence,
          resume_after, terminal,
          enumeration_intact, connector_version, committed_at)
job_page_batches(job_id, checkpoint_lineage, partition_key, page_id, page_sequence, dataset,
                 contract_version, projector_version,
                 object_key, sha256, size, records)
job_artifacts(job_id, dataset, contract_version, kind,
              location, sha256, size, published_at)
```

Primary/unique keys include checkpoint lineage, page, dataset, contract version,
and projector version and enforce idempotency. Dataset state is
independent: collection can succeed while one projector fails, and successful
datasets remain publishable. Aggregate job status and the operator UI summarize
these rows; they do not erase the distinction. Enumeration completeness is a
collection fact, while projection, loading, and promotion are dataset facts.
Each dataset independently declares whether an incomplete collection permits an
adds-only load and whether it ever permits retirement.

Define the aggregate-state matrix as part of the schema/API contract: all
requested outputs successful means succeeded; at least one usable output plus a
degraded or failed output means degraded; no usable requested output means
failed; cancellation remains cancellation even when committed page batches
exist. The control API exposes dataset output rows explicitly rather than
flattening their diagnostics into the job's first error.

`degraded` becomes an explicit terminal value of `catalogue.jobs.state`, not
only a summary label. The additive migration updates the jobs check constraint
and every closed enumeration of terminal/success states: worker finishing and
events, `ops.runs.TERMINAL` and run tallies, claim/reap/retry logic, control API
models and queries, monitoring, comparison eligibility, retention, promotion,
source-history selection, and proxy-route success evidence. A degraded job is
terminal and retryable by operator policy. Its aggregate state alone is not a
comparison or promotion signal, but each complete successful dataset remains
eligible independently. Run summaries count degraded jobs separately and derive
run status from succeeded/degraded/failed/cancelled/skipped counts.

If a projector fails on a page, its dataset becomes failed for that lineage and
the pipeline stops invoking that projector while continuing collection and the
remaining projectors. The failed dataset is not silently retried from later
pages. Retrying it requires replayable neutral snapshots or a new collection;
that choice is explicit so fetch reuse never turns into missing earlier pages.

Migrate artifact comparison, retention, download, run detail, progress, and
promotion readers to `job_artifacts` and `job_datasets` before those tables
become authoritative. Retention operates per immutable output artifact and may
not delete page batches referenced by an active checkpoint lineage or an
unpublished artifact.

### 12.5 Artifact store

Introduce an `ArtifactStore` protocol with local-filesystem and later object-
store implementations. It provides attempt-scoped staging, immutable publish,
checksum verification, streaming reads, and bounded orphan cleanup. Artifact
locations are opaque store locations rather than paths assumed to be locally
mounted.

The local implementation preserves current paths and atomic rename behavior.
Before workers run on more than one node, deployment must use either a correctly
mounted shared filesystem or an object-store implementation. A database row is
published only after the referenced immutable object is readable and its digest
matches.

### 12.6 Bounded-memory pipeline

Connectors, projectors, artifact writers, and loaders accept pages or bounded
batches. The new pipeline must never convert the complete stream back to a list.
Its target peak memory is `O(page size × enabled projectors)`, independent of
total source size. Compatibility adapters may accumulate legacy results only
for explicitly bounded legacy jobs and disappear with the legacy path.

Phase benchmarks include a synthetic source at least ten times larger than the
largest current source and demonstrate that peak memory reaches a plateau.

## 13. Migration phases

### Phase 0: baselines and contracts

Deliver:

- architecture decision record for connector/entity/projector boundaries;
- frozen golden artifacts for representative sources on every active platform;
- per-platform request, record, field-coverage, and byte baselines;
- typed neutral commerce models;
- connector capability and dataset projector protocols;
- backend-neutral browser protocol, backend capability vocabulary, and typed
  operator browser-profile configuration;
- compatibility adapter that still returns `ScrapeResult`;
- page identity, atomic commit, checkpoint-lineage, and artifact-store ADR;
- additive multi-dataset job/page/artifact schema, `degraded` job state,
  `requires_any`, and selected-browser-backend snapshot;
- compatibility reads in control, retention, comparison, progress, and
  promotion for per-dataset output state;
- CI checks for serialization and deterministic projection.

Exit criteria:

- no production behavior changes;
- old and new contracts can coexist in one process;
- a crash at every boundary around staging, page commit, compaction, and publish
  has a specified idempotent recovery outcome;
- the baseline comparison names changed, missing, and newly populated fields.

### Phase 1: ceramics projector extraction

Deliver:

- `CeramicsCatalogueProjector` wrapping existing `record.build()` behavior;
- `CeramicsIdentityProjector` preserving the active
  `ceramics.catalogue_identity.v2` manufacturer-without-price path;
- scope and enrichment configuration passed through projection context;
- projector-level tests independent of network/platform payloads;
- typed dataset records before conversion to legacy dictionaries;
- runner support for selecting a dataset while defaulting to the current one.

Exit criteria:

- a synthetic neutral product projects to the same v2 rows as direct
  `record.build()` calls;
- the Mayco identity-only golden fixture retains its identity format,
  manufacturer references, specifications, and stable IDs without requiring a
  price or purchasable variant;
- stable IDs, validity, enrichment, and scope output are unchanged;
- no platform scraper has migrated yet.

### Phase 1.5: browser backend abstraction and CDP extension proxy

Deliver:

- adapt the existing `BrowserRenderer` behind process-owned `BrowserBackend` and
  job-owned `BrowserSession` protocols without changing current Camoufox
  behavior;
- add an optional Playwright Chromium adapter that connects through the
  browser-level endpoint provided by
  `projects-caddy/cdp-extension-proxy`;
- declare Playwright as a direct, pinned optional dependency for this adapter
  rather than relying on Camoufox's transitive packages; the worker attaches to
  the external Chromium and does not download or launch another Chromium;
- add worker capability advertisement and exact-backend queue routing;
- add all-of/any-of capability claiming and atomically snapshot the backend
  selected for an `auto` job;
- add operator-owned logical CDP profiles with secret token resolution and
  redaction;
- add a `CdpEndpointProvider` and a bounded disposable/pre-warmed production
  pool; keep static endpoints development-only;
- extend `projects-caddy/cdp-extension-proxy` readiness metadata or trusted
  deployment attestation with service version, instance id, route mode,
  isolation mode, and clean profile generation;
- acquire a distributed endpoint-capacity lease before attaching;
- provision or lease a disposable clean CDP instance/profile per job until true
  isolated browser contexts pass the shared backend contract;
- create, attribute, close, and detach targets inside that isolation boundary,
  including cancellation, timeout, worker shutdown, and WebSocket-loss paths;
- enforce direct-only CDP routing and verify route/isolation/service-generation
  attestation during readiness;
- expose backend health, attach latency, active targets, disconnects, and cleanup
  failures without logging endpoint tokens;
- retain Camoufox as the default until source-specific Chromium compatibility is
  demonstrated by recorded or controlled tests.

Exit criteria:

- the same browser session contract tests pass against Camoufox and the CDP
  proxy for render, evaluated JSON, in-page JSON request, timeout, and cleanup;
- a worker without the selected backend cannot claim the job;
- concurrent jobs cannot attach to an MVP endpoint beyond its declared
  capacity;
- sequential jobs cannot observe cookies, storage, cache, service workers, or
  authenticated state from an earlier job;
- target and connection loss produces an incomplete, retryable outcome and
  leaves no orphaned catalogue-owned tab, instance, or user-data directory;
- tokens never appear in configuration projections, logs, diagnostics,
  checkpoints, artifacts, metrics, or trace attributes;
- production startup rejects unauthenticated, publicly exposed, proxy-routed,
  persistently shared, or unattested CDP profiles;
- no connector imports backend-specific libraries or CDP commands.

### Phase 2: Shopify pilot

Shopify is first because it is structured, heavily used, and now exercises
pagination, exact-stock enrichment, rate-limit fallback, session rotation, and
proxy budgets.

Deliver:

- `ShopifyConnector` emitting neutral products and variants;
- Shopify typed options;
- capability declarations;
- prioritized feed/detail requests;
- cursor checkpoints at `products.json` page boundaries;
- page-batch staging and reconstruction of final artifacts across attempts;
- compatibility adapter selecting the ceramics projector;
- replay comparison against Ulster, Ceradel, and at least one shop that exposes
  only the normal Shopify feed.

Exit criteria:

- record IDs and published field values have golden parity;
- no additional requests or proxy bytes beyond an agreed measured tolerance;
- Ulster remains complete under its 10 MB limit with optional-stock notes, not
  errors;
- cancellation after a page resumes without refetching earlier pages;
- forced worker death before and after every page-commit step loses and
  duplicates no output;
- peak memory remains bounded as synthetic product count increases;
- old Shopify implementation remains selectable for canary rollback.

Rollout:

1. replay the same recorded raw responses through the legacy scraper and the
   new connector/projector path;
2. run the new path as a production shadow without loading and compare it to
   the most recent trusted legacy artifact for that source;
3. compare summaries, artifacts, request plans, and field-level reasons;
4. enable a small source allowlist;
5. expand after two successful scheduled cycles;
6. remove the old path only after all Shopify sources pass.

### Phase 3: structured commerce platforms

Migrate in measured order:

1. WooCommerce;
2. BigCommerce;
3. Wix;
4. PrestaShop;
5. Shopware, SumUp, Starweb, and NitroSell.

For each connector:

- inventory existing configuration keys and assign a typed owner;
- extract platform payload mapping from ceramics projection;
- declare capabilities and refresh modes;
- implement fixture and malformed-payload tests;
- compare request count, record count, stable IDs, and field coverage;
- run shadow/canary rollout;
- retain a rollback selector until two scheduled cycles succeed.

Exit criteria per connector:

- all active sources migrated or explicitly documented as exceptions;
- no platform name remains in dataset projection or runner branching;
- platform-specific parsing tests do not need ceramics enrichment to pass.

### Phase 4: generic and bespoke page scrapers

Generic HTML is less uniform and must not be forced into false platform
abstractions.

Deliver:

- a `PageCommerceConnector` composed from discovery strategies and parsers;
- discovery strategies for sitemap, category pagination, and product links;
- parsers for JSON-LD, microdata, OpenGraph, and verified DOM selectors;
- typed parser outcomes distinguishing unsupported markup from browser need;
- configuration-owned selector/parser rules where appropriate;
- bespoke connectors retained for sites whose behavior is genuinely unique.

Do not replace a clear 100-line bespoke connector with a large declarative
language. Reuse transport, entities, projection, diagnostics, and budgets even
when parsing stays site-specific.

Exit criteria:

- generic parser-empty results do not automatically trigger browser work;
- discovery completeness is represented consistently;
- bespoke connectors emit the same neutral snapshots as platform connectors.

### Phase 5: additional datasets

Add one dataset at a time to prove reuse.

#### 5.1 Stock observations

- emit only exact quantities with evidence;
- retain availability when quantity is unknown;
- treat missing required exact-stock work as partial/degraded stock output even
  when the catalogue output succeeds;
- use change-based storage with `first_seen_at`/`last_seen_at`;
- expose coverage by source and evidence method;
- schedule expensive stock work independently from ordinary catalogue refresh.

#### 5.2 Price observations

- consume neutral offers without ceramic projection;
- preserve VAT, currency, quantity context, and variant identity;
- use the existing change-log semantics;
- permit lightweight daily price collection where capabilities allow it.

#### 5.3 Documents

- emit product-to-document relationships with URL, label, media type, language,
  and evidence;
- do not download documents by default;
- add a separate, bounded document-fetch workflow if content indexing is later
  approved.

Exit criteria:

- one Shopify fetch can produce multiple datasets without duplicate remote
  requests;
- disabling one dataset removes its optional request requirements;
- dataset failures are isolated and cannot invalidate successful projections
  unless they affect enumeration completeness.

### Phase 6: configuration and legacy removal

Deliver:

- mechanical migration of `sources.json` to sectioned typed configuration;
- control-plane overlay migration;
- removal of legacy dictionary projection after all readers migrate;
- removal of old scraper implementations and rollback flags;
- move generic fetch classes into `transports/` if the move still improves
  ownership;
- update operator and contributor documentation.

Exit criteria:

- no scraper reads arbitrary `dict[str, Any]` configuration;
- no connector imports ceramics record or enrichment modules;
- no dataset projector imports platform-specific payload types;
- runner, scheduler, and worker select components from capabilities/registries;
- unused legacy fields fail validation.

## 14. Testing strategy

### 14.1 Test pyramid

1. **Model tests:** validation, serialization, money, stock semantics, evidence.
2. **Connector mapping tests:** recorded platform payload to neutral snapshots.
3. **Projector tests:** neutral snapshots to dataset records.
4. **Pipeline tests:** paging, fan-out, priorities, cancellation, checkpoints.
5. **Golden tests:** old and new final ceramics artifacts.
6. **PostgreSQL tests:** idempotency, partial updates, retirement, observations.
7. **Deployment smoke tests:** worker capability, control run, health, artifact.
8. **Browser contract tests:** the same render/evaluate/in-page-request suite
   against Camoufox and a controlled `cdp-extension-proxy` instance.

The golden set includes both purchasable catalogue items and the active Mayco
`ceramics.catalogue_identity.v2` path; parity is not complete if only priced
offers match.

### 14.2 Fixture policy

- CI never contacts live shops or Decodo;
- sanitize cookies, tokens, customer/cart data, and proxy information;
- record response status, relevant headers, and compressed body;
- keep fixtures small but include real pagination and variant edge cases;
- version fixtures when a platform schema changes;
- retain a malformed fixture for every production schema incident.

### 14.3 Parity comparison

Compare semantically rather than by list order:

- stable record ID set;
- published values by record ID;
- derived values by module version;
- field coverage;
- records kept/dropped by scope and reason;
- requests by transport and priority;
- transferred/proxy bytes;
- completeness, diagnostics, and retirement permission.

Intentional differences require a checked-in decision describing why the old
or new behavior is correct.

### 14.4 Property and fault tests

Add tests proving:

- projection is deterministic and does not mutate neutral snapshots;
- pagination cursors cannot skip or duplicate pages during retry;
- a healthy non-terminal page never marks enumeration incomplete, and retirement
  requires an intact committed terminal outcome;
- crashes before staging, after staging, before database commit, after database
  commit, during compaction, and after artifact publication are recoverable;
- optional budget exhaustion cannot stop discovery;
- connector cancellation leaves a resumable page-boundary checkpoint;
- an invalid checkpoint cannot corrupt a full run;
- one projector failure does not discard other dataset output;
- repeated page commits and dataset promotion are idempotent;
- exact stock is never produced from an unverified order limit;
- an unobserved or carried-forward offer/stock value cannot produce a fresh
  observation record;
- required stock budget exhaustion degrades only the stock dataset while
  optional stock exhaustion remains a catalogue note;
- partial refresh cannot erase fields owned by another dataset;
- secrets cannot enter entities, checkpoints, diagnostics, or artifacts.
- CDP disconnect, target close, extension detach, and worker cancellation always
  release endpoint leases and attempt tab cleanup;
- sequential CDP jobs cannot observe each other's cookies, storage, cache, or
  service workers;
- CDP readiness rejects a proxy-routed, persistent-profile, incorrectly
  generated, or unattested service;
- `auto` jobs are claimable by either allowed exact backend, record one selected
  backend atomically, and never treat backend alternatives as all-of requirements;
- an untrusted run cannot choose a CDP endpoint, token, target, method, script,
  or off-source navigation URL.

## 15. Observability

Add dimensions without unbounded-cardinality labels:

- connector, connector version, dataset, and contract version;
- entities discovered, normalized, projected, rejected, and loaded;
- requests/bytes/time by priority and transport;
- optional work planned, completed, skipped, and deferred;
- checkpoint created, resumed, rejected, and completed;
- stock coverage by quantity kind and evidence method;
- projector failures by stable diagnostic code;
- parity differences during shadow rollout.
- browser operations, failures, attach latency, and active targets by backend;
- CDP endpoint lease contention, disconnects, detach events, and cleanup
  failures by bounded logical profile name.

URLs, entity IDs, product names, cursors, and exception bodies remain in bounded
job diagnostics rather than metric labels.

CDP endpoint URLs, WebSocket URLs, and query strings are also excluded from
logs, diagnostics, metrics, and traces because the proxy token may be carried in
the query string. Observability records only the logical profile and backend.

The operator view should answer:

- did enumeration complete;
- which datasets were produced;
- which optional fields were skipped and why;
- how much direct/browser/proxy traffic each priority used;
- whether the run resumed;
- whether the connector schema appears to have changed;
- which browser backend and logical profile ran, whether it is healthy, and
  whether endpoint capacity or cleanup affected the job.

## 16. Rollout and compatibility policy

Every connector uses the same rollout ladder:

1. unit and replay tests;
2. local golden parity;
3. production shadow projection with no load;
4. source allowlist canary;
5. two successful scheduled cycles;
6. default new path with old rollback flag;
7. remove old path after a documented observation window.

The legacy platform scraper is not a projector over neutral snapshots, so the
plan must not claim that two projections over the new connector's output prove
extraction parity. Extraction parity is established offline by replaying the
same frozen raw responses through both complete paths. Production shadow mode
runs only the new live path and compares its output with the last trusted legacy
artifact, accounting for expected source changes by observation time and stable
identity. If a future legacy adapter can consume captured raw responses without
a second network request, it may run in shadow too. Never crawl a live source
twice merely to compare implementations.

The artifact-only gate is `catalogue-shadow-compare`. It streams existing
legacy and connector NDJSON/NDJSON.gz artifacts through a bounded temporary
index keyed by stable record identity, emits deterministic redacted JSON, and
uses reviewed versioned ignore/tolerance rules. Optional existing job summaries
add record and request metadata to the same gate; the command never invokes a
scraper or transport.

Database migrations are additive. Readers accept both old and new metadata
before writers switch. Removal happens only after all deployed readers use the
new contract.

## 17. Security and safety invariants

The migration must preserve:

1. no secrets in entities, artifacts, checkpoints, logs, traces, or metrics;
2. direct access by default and operator-owned proxy enablement;
3. PostgreSQL/Decodo ledgers as final paid-traffic authority;
4. robots and host pacing independent of dataset demand;
5. no retirement from an incomplete enumeration;
6. no live external traffic in deterministic tests;
7. no browser fallback merely because a parser returned no entity;
8. no arbitrary code, selectors, or URLs accepted from ordinary run requests;
9. exact stock only from published or explicitly verified evidence;
10. connector extensions treated as untrusted external data and size-bounded;
11. CDP endpoints and tokens resolved only from operator-owned worker secrets;
12. production CDP profiles authenticated and reachable only through loopback or
    a private network boundary;
13. no arbitrary CDP command, JavaScript, browser target, or navigation URL from
    ordinary run requests;
14. a browser session never supplies authenticated state to dataset output;
15. CDP jobs use disposable profiles or contract-tested isolated contexts, never
    a sequentially shared persistent default context;
16. CDP traffic is direct-only until it participates in the same reservation,
    enforcement, accounting, and reconciliation authority as other paid traffic.

## 18. Risks and mitigations

### Over-generalization

Risk: a complex abstraction makes simple platform code harder to understand.

Mitigation: keep the connector protocol small, permit bespoke connectors, and
require two real implementations before extracting a shared helper.

### Output drift

Risk: moving record construction changes stable IDs, filtering, or inferred
fields.

Mitigation: frozen artifacts, semantic parity, shadow projection, and separate
approval for intentional corrections.

### Increased memory

Risk: collecting neutral snapshots before projection duplicates large payloads.

Mitigation: bounded page streams, streaming projectors and sinks, durable page
batches, and a scaling benchmark; never retain a whole source in process memory.

### Checkpoint/output split brain

Risk: a crash between writing projected output and advancing a cursor loses or
duplicates a committed page.

Mitigation: stable page identities, content-addressed staging, a transactional
page manifest plus cursor advance, idempotent compaction, and fault injection at
each boundary.

### Multi-dataset partial failure

Risk: one failed projector makes a successful dataset appear failed, or a green
aggregate job hides a missing dataset.

Mitigation: dataset-specific state, artifacts, completeness, promotion, and
operator-visible errors with an explicitly derived aggregate job state.

### Multi-node artifact visibility

Risk: a worker publishes a local path that the loader or control service on
another node cannot read.

Mitigation: require shared storage or an `ArtifactStore` implementation before
multi-node rollout, and verify object readability and checksum before publish.

### CDP endpoint compromise or session leakage

Risk: a token-bearing CDP URL leaks through telemetry, an exposed endpoint gives
control of Chromium to another network peer, or a shared profile carries cookies
between source jobs.

Mitigation: operator-owned secret profiles, production token requirement,
loopback/private-network validation, aggressive URL redaction, capacity-one
distributed leases for the current MVP, a disposable clean browser profile or
contract-tested isolated context per job, cleanup on every exit path, and no
reliance on persistent authenticated browser state. A fresh tab in the default
context is not isolation.

### CDP egress escapes paid-traffic authority

Risk: the external Chromium uses its own configured proxy and sends navigation
or subresource traffic outside the catalogue reservation and byte ledger.

Mitigation: production CDP profiles are direct-only and attest their route.
Proxy-routed CDP is enabled only after the service binds each job to a database
reservation, enforces its route and ceiling, meters all relevant traffic, fails
closed on expiry, and reconciles provider usage.

### CDP compatibility drift

Risk: the extension proxy implements a documented CDP subset rather than every
browser-level command expected by a new Playwright or Chromium release.

Mitigation: pin and test the client/browser/proxy compatibility matrix, probe
`/json/version` and required commands at worker readiness, advertise the backend
capability only after the probe passes, and fall back to Camoufox only when the
source permits that backend.

### Duplicate requests

Risk: multiple datasets independently ask for the same detail page.

Mitigation: union capability requirements before collection, request once, and
fan out the normalized entity.

### Configuration migration errors

Risk: unset-versus-false defaults change behavior.

Mitigation: legacy projection golden tests for every source and a mechanical,
reviewable migration.

### Checkpoint incompatibility

Risk: resuming after code/config changes mixes two meanings of a cursor.

Mitigation: version and fingerprint checkpoints; reject rather than guess.

### Optional work hides quality loss

Risk: a green source silently loses stock or documents.

Mitigation: dataset-specific coverage, skipped counts, thresholds, and operator
alerts separate from source enumeration success.

## 19. Scaling and capacity

### 19.1 Capacity model before autoscaling

Record and review these limits per deployment:

- runnable jobs and oldest queue age by required worker capability;
- HTTP job slots, browser processes/pages, and measured memory per active page;
- Camoufox page capacity and CDP-extension-proxy endpoint/target capacity by
  logical profile;
- PostgreSQL pool size, active connections, claim latency, transaction latency,
  and page-manifest write rate;
- artifact staging, compaction, and read throughput;
- host and shared-edge lease contention, response latency, 429 rate, and
  published rate limits;
- direct, browser, and paid-proxy byte ceilings.

Worker slot defaults must fit the PostgreSQL connection budget and worst-case
page memory, not merely CPU count. Autoscaling uses oldest runnable-job age and
capability-specific saturation. Raw queue length is insufficient because jobs
blocked on host leases or unavailable capabilities are not runnable capacity.

### 19.2 Scale across independent sources first

The existing PostgreSQL `FOR UPDATE SKIP LOCKED` queue, job leases, and host
leases remain the horizontal-scaling mechanism. Add stateless worker replicas
and separate capability pools:

- HTTP-only workers for ordinary connectors;
- Camoufox workers with lower job concurrency and explicit memory limits;
- `cdp_extension_proxy` workers attached only to their operator-configured
  pool of disposable clean Chromium services and bounded by endpoint leases;
- projection/loading workers only if measurements show those stages dominate.

More workers increase throughput across independent sources. They must not
multiply traffic to one origin or storefront provider. Host and shared-edge
politeness remains globally enforced; in-process adaptive pacing is an
additional local safeguard, not the fleet-wide authority.

### 19.3 Split stages only after measurement

Collection and projection initially remain in one leased job and communicate
through bounded pages. If CPU projection, document processing, or loading
becomes the bottleneck, committed page batches may become durable work inputs
for independently leased dataset tasks. PostgreSQL remains the queue; no new
broker is required.

Stage separation must preserve the collection lineage, projector version,
idempotency key, dataset state, cancellation semantics, and paid-traffic
authority. It must never cause a second remote fetch merely because a projector
worker retried.

### 19.4 Partition exceptional sources

A single source remains one sequential connector stream by default. Adding
workers cannot shorten it. A connector may optionally declare stable,
independently resumable partitions only when the remote platform exposes a safe
boundary, such as sitemap shards, disjoint collections, API partitions, or a
completed discovery manifest of product identifiers.

Each partition has a stable key, cursor, lease, page manifests, and deduplication
rule. A parent collection is complete only after the expected partition set is
sealed and every partition reaches its terminal cursor. Missing or changed
partitions withhold retirement. All partitions still share global host/edge
pacing and request budgets; partitioning is not permission to exceed a remote
service's rate limit.

### 19.5 Storage and database growth

Use the local artifact store for one-node deployments and shared/object storage
before adding nodes. Apply bounded retention to orphan staging objects and
committed page batches after immutable artifact publication and checkpoint
expiry. Observation and history tables use batch writes, change-based rows, and
appropriate time/source partitioning only when measured table or index growth
justifies it.

Scaling is accepted only when a load test demonstrates no record loss or
duplication, bounded memory, stable database latency, globally respected host
pacing, and correct recovery after worker termination.

## 20. Implementation order by repository path

1. Write the boundary, page-commit/recovery, multi-dataset state, artifact-store,
   and capacity ADRs.
2. Add contracts under `catalogue-dump/src/mb_ceramics_catalogue/connectors/`
   and `datasets/` with unit tests.
3. Add the job-dataset, page-manifest, batch, and artifact schema plus
   `requires_any`, `selected_browser_backend`, and terminal `degraded` job-state
   migrations across every worker/control consumer.
4. Add a pipeline compatibility adapter that returns the existing
   `ScrapeResult` and summary shape.
5. Extract the ceramics catalogue and identity projectors while leaving
   `record.py` as their internal implementation.
6. Extend the registry to distinguish connectors from legacy scrapers.
7. Add capabilities and remove runner platform-name branching.
8. Introduce `BrowserBackend`/`BrowserSession`, adapt Camoufox, then integrate
   `projects-caddy/cdp-extension-proxy` with capability routing, operator secret
   profiles, all-of/any-of claims, direct-route attestation, disposable profile
   isolation, endpoint leases, contract tests, and cleanup fault tests.
9. Add request priorities to `Fetcher` and proxy budget planning.
10. Implement the local `ArtifactStore`, bounded page pipeline, atomic page
   commit protocol, compaction, and fault-injection tests.
11. Implement Shopify connector, replay fixtures, checkpointing, and shadow
   comparison.
12. Roll Shopify out and retire only its legacy path.
13. Repeat for structured platforms.
14. Compose generic page discovery/parsers and migrate bespoke scrapers.
15. Add stock, price, and document datasets with storage migrations.
16. Migrate `sources.json` and control overlays to discriminated options.
17. Add stage separation, source partitioning, or an object store only when the
    capacity measurements in section 19 require them.
18. Remove compatibility code and update documentation.

Do not combine more than one platform migration in a commit. A platform commit
must be revertible without reverting contracts already used by another one.

## 21. Completion checklist

Checked items are implemented and test-verified in this repository. Unchecked
items require production/operator evidence or deliberate post-rollout removal;
they are not implied complete by the canary implementation.

- [x] Neutral commerce and evidence contracts are versioned and documented.
- [x] Connector and dataset protocols are stable.
- [x] Camoufox and `cdp_extension_proxy` pass the same browser session contract
  and jobs route only to workers advertising the selected backend.
- [ ] CDP profiles are operator-owned, authenticated, redacted, capacity-leased,
  direct-routed, isolation-attested, health-checked, and clean up disposable
  profiles and catalogue-owned targets on every exit path.
- [x] `auto` browser jobs use any-of capability claims and persist exactly one
  selected backend before collection.
- [x] Ceramics projection is platform-independent.
- [x] `ceramics.catalogue_identity.v2` retains Mayco parity without requiring a
  price or purchasable variant.
- [ ] Shopify golden parity and production rollout are complete.
- [ ] WooCommerce golden parity and production rollout are complete.
- [x] Remaining structured platform code migrations are complete behind
  explicit canaries.
- [x] Generic and bespoke scrapers emit neutral snapshots.
- [x] Runner contains no scraper-name feature switches.
- [x] Source configuration uses typed connector/dataset sections.
- [x] Discovery and dataset-derived required/detail/optional budgets are
  enforced independently for every requested output.
- [x] Page-boundary checkpoints resume safely for every registered runtime,
  including declared-order and hashed PrestaShop/SIO-2 partitions.
- [x] Page terminality is independent of enumeration integrity, and retirement
  requires an intact committed terminal outcome.
- [x] Atomic page commits survive fault injection without loss or duplication.
- [x] Pipeline memory is bounded independently of total source size.
- [x] Dataset-specific state and artifacts expose partial success accurately.
- [x] `degraded` is a terminal job state understood consistently by workers,
  runs, control APIs, monitoring, retention, comparison, promotion, history, and
  proxy evidence.
- [x] Control, progress, comparison, retention, download, load, and promotion
  consume per-dataset output state.
- [x] Offers, stock, and documents cannot be emitted as newly observed unless
  their snapshot carries a current observation time and evidence.
- [ ] Artifact locations are readable by every deployed consumer.
- [ ] Capacity and recovery load tests pass at the intended worker count.
- [x] Stock, price, and document datasets reuse one collection stream.
- [x] Existing ceramics API output remains contract-compatible.
- [ ] Raw-response replay compares legacy and new extraction paths, and live
  shadow rollout compares against trusted legacy artifacts without a second
  crawl.
- [ ] All old paths and temporary rollback flags are removed.
- [ ] Operator documentation and contributor examples are current.
- [ ] Full unit, golden, PostgreSQL, service, and deployment smoke suites pass.

## 22. Definition of fully migrated

The code is fully migrated only when the dependency directions are true in
both source code and tests:

```text
workers/control → pipeline → connectors → transport
                         └→ datasets → domain enrichments
```

- connectors do not import dataset or ceramics modules;
- dataset projectors do not import platform payload implementations;
- transport does not know about products, ceramics, or datasets;
- workers and control operate on connector capabilities and dataset contracts,
  not platform-specific conditionals;
- storage sinks consume versioned dataset records;
- every active source runs through the new pipeline;
- compatibility adapters and legacy configuration are gone.

Until all of those statements are true, the status of this plan remains
`in progress`, even if the largest platforms have already moved.
