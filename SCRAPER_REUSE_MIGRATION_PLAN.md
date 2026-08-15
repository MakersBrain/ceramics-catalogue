# Reusable scraping architecture migration plan

Status: proposed; implementation not started
Prepared: 2026-08-15
Scope: `catalogue-dump`, catalogue workers, control-plane source configuration,
artifacts, PostgreSQL loading, monitoring, and platform scraper tests.

## 1. Outcome

Turn the current ceramics-specific scraper collection into a reusable data
collection platform without rewriting the working transport, proxy, scheduling,
or platform integrations.

The migration is complete when:

- Shopify, WooCommerce, Wix, BigCommerce, PrestaShop, and the generic page
  crawler extract into typed, platform-neutral commerce entities;
- ceramic classification and enrichment consume neutral entities through a
  dataset projector instead of being called directly by platform scrapers;
- one platform crawl can produce catalogue items, price observations, stock
  observations, document links, and future datasets without repeating
  discovery or fetching;
- adding a retail vertical requires a projector and enrichment modules, not a
  copy of each platform scraper;
- adding a platform requires a connector and mapping tests, not changes to the
  ceramics schema or runner;
- pagination, essential product data, and optional enrichment have declared
  priorities and independent budgets;
- large crawls can checkpoint and resume from a stable cursor;
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
     CommerceEntity stream
 product / variant / offer / stock / document
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
  transport/                 # eventual home of generic fetch infrastructure
    protocols.py
    priorities.py
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

Neutral entities contain published source facts, not ceramic interpretation.
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
    options: dict[str, str] = {}
    offer: CommerceOffer | None = None
    published_attributes: dict[str, JsonValue] = {}


class CommerceProduct(BaseModel):
    source_id: str
    external_id: str
    canonical_url: str
    title: str
    description: str | None = None
    vendor: str | None = None
    categories: list[CategoryRef] = []
    images: list[MediaRef] = []
    documents: list[DocumentRef] = []
    variants: list[CommerceVariant]
    source_updated_at: datetime | None = None
    platform_extensions: dict[str, JsonValue] = {}
```

Use `Field(default_factory=...)` in the implementation rather than mutable
literal defaults shown compactly above.

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
    evidence: list[Evidence]
```

Only `quantity_kind="exact"` may populate the current catalogue's
`stock_quantity`. Cart limits and order-policy ceilings remain separate facts.

### 6.4 Collection pages and cursors

Connectors return pages rather than appending directly to `ScrapeResult`:

```python
class EntityPage(BaseModel, Generic[T]):
    items: list[T]
    cursor: JsonValue | None = None
    complete: bool
    discovered: int
    diagnostics: list[Diagnostic] = []
```

The cursor is connector-owned but JSON-serializable. `complete=False` has the
same retirement meaning as today's `truncated=True`.

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
    ) -> AsyncIterator[EntityPage[CommerceProduct]]: ...
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
    entity_kinds: frozenset[EntityKind]
    refresh_modes: frozenset[RefreshMode]
    stock_kinds: frozenset[StockQuantityKind]
    supports_incremental_cursor: bool
    supports_category_filter: bool
    supports_documents: bool
    browser: Literal["never", "optional", "required"]
    shared_edge: str | None
```

The runner asks whether the requested dataset and refresh mode are supported.
It never checks `config.scraper in {...}`.

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

## 8. Dataset and projector contracts

### 8.1 Dataset registry

Add a registry parallel to the connector registry:

```python
class DatasetDefinition(Protocol):
    name: str
    version: str
    required_entities: frozenset[EntityKind]
    required_capabilities: frozenset[str]

    def project(self, entity: CommerceProduct, context: ProjectionContext) \
            -> Iterable[DatasetRecord]: ...
```

Initial registrations:

- `ceramics.catalogue_item.v2`;
- `commerce.price_observation.v1`;
- `commerce.stock_observation.v1`;
- `commerce.document.v1`.

### 8.2 Ceramics compatibility projector

Move the behavior currently reached through `record.build()` into a ceramics
projector in stages:

1. map neutral identity, offer, stock, media, category, and attribute fields;
2. invoke the selected ceramics enrichment modules;
3. apply ceramics scope and exclusions;
4. call the existing record validation and stable-ID functions;
5. emit exactly `ceramics.catalogue_item.v2`.

Initially the projector may call `record.build()` internally. Once all major
connectors use it and parity is proven, split `record.py` into contract,
projection, enrichment, and validation modules.

### 8.3 Multiple outputs from one crawl

The pipeline should fetch once and project many times:

```text
CommerceProduct
  ├── ceramics catalogue rows
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

## 9. Typed source configuration

### 9.1 Separate concerns

Replace the growing flat model with three validated sections while retaining a
loader for the current JSON shape:

```json
{
  "source": {"label": "...", "url": "...", "country": "GB"},
  "crawl": {"delay": 0, "timeout_seconds": 3600},
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

## 10. Priority-aware request budgeting

### 10.1 Priority classes

Generalize the Shopify feed reserve into transport priorities:

1. `DISCOVERY`: robots, sitemap, catalogue/category pagination;
2. `IDENTITY`: product identity required for a valid base record;
3. `OFFER`: price and availability required by the requested dataset;
4. `DETAIL`: description, images, technical tables, and documents;
5. `OPTIONAL`: exact-stock probes or other expensive enrichments.

The ordering expresses data completeness, not request urgency. Optional work
may use remaining capacity but may never consume a reservation needed for
discovery.

### 10.2 Budget object

Introduce a per-job `RequestBudget` with:

- direct request/time allowance;
- browser request/time allowance;
- paid-proxy bytes reserved and used;
- per-priority reserves and ceilings;
- estimates updated from observed response sizes;
- a decision method returning allow, downgrade, defer, or deny.

The first implementation wraps existing counters and proxy leases. It must not
create a second spending authority: the PostgreSQL/Decodo reservation remains
the hard limit.

### 10.3 Planning optional work

After each page, estimate optional detail cost from observed mean and a
conservative high percentile. Stop optional work before the protected discovery
reserve. Record skipped work as a note/metric, not a source error.

Static constants such as the current 1 MB Shopify reserve remain as a safe
fallback until enough measurements exist. Learned estimates may reduce work,
never override the hard reservation.

## 11. Checkpointing and resumability

### 11.1 Checkpoint contents

A checkpoint contains:

- source, connector, and connector version;
- normalized configuration fingerprint;
- current enumeration cursor;
- completed entity/page identifiers where required for idempotency;
- selected datasets and their contract versions;
- observed budget state;
- creation time and expiry;
- checksum.

Never store proxy credentials, cookies, authenticated URLs, or browser storage
in a checkpoint.

### 11.2 Persistence

Persist checkpoints after a complete page, not after every record. Store the
small current checkpoint in PostgreSQL and include its identifier in job
events. Completed artifacts remain the durable audit representation.

### 11.3 Resume rules

Resume only when connector version, source URL, relevant configuration
fingerprint, and dataset contract are compatible. Otherwise start from the
beginning and retain the rejected checkpoint for diagnosis.

A resumed full crawl is not complete until it reaches the connector's terminal
cursor. Retirement remains withheld for cancelled, expired, or incomplete
checkpoint chains.

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

Do not enable permanent neutral-entity storage by default. It can multiply
storage and duplicate source payloads. Initially:

- keep neutral entities in memory/page streams;
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

## 13. Migration phases

### Phase 0: baselines and contracts

Deliver:

- architecture decision record for connector/entity/projector boundaries;
- frozen golden artifacts for representative sources on every active platform;
- per-platform request, record, field-coverage, and byte baselines;
- typed neutral commerce models;
- connector capability and dataset projector protocols;
- compatibility adapter that still returns `ScrapeResult`;
- CI checks for serialization and deterministic projection.

Exit criteria:

- no production behavior changes;
- old and new contracts can coexist in one process;
- the baseline comparison names changed, missing, and newly populated fields.

### Phase 1: ceramics projector extraction

Deliver:

- `CeramicsCatalogueProjector` wrapping existing `record.build()` behavior;
- scope and enrichment configuration passed through projection context;
- projector-level tests independent of network/platform payloads;
- typed dataset records before conversion to legacy dictionaries;
- runner support for selecting a dataset while defaulting to the current one.

Exit criteria:

- a synthetic neutral product projects to the same v2 rows as direct
  `record.build()` calls;
- stable IDs, validity, enrichment, and scope output are unchanged;
- no platform scraper has migrated yet.

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
- compatibility adapter selecting the ceramics projector;
- replay comparison against Ulster, Ceradel, and at least one shop that exposes
  only the normal Shopify feed.

Exit criteria:

- record IDs and published field values have golden parity;
- no additional requests or proxy bytes beyond an agreed measured tolerance;
- Ulster remains complete under its 10 MB limit with optional-stock notes, not
  errors;
- cancellation after a page resumes without refetching earlier pages;
- old Shopify implementation remains selectable for canary rollback.

Rollout:

1. replay only;
2. shadow projection in production without loading;
3. compare summaries and artifacts;
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
- bespoke connectors emit the same neutral entities as platform connectors.

### Phase 5: additional datasets

Add one dataset at a time to prove reuse.

#### 5.1 Stock observations

- emit only exact quantities with evidence;
- retain availability when quantity is unknown;
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
- move generic fetch classes into `transport/` if the move still improves
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
2. **Connector mapping tests:** recorded platform payload to neutral entities.
3. **Projector tests:** neutral entities to dataset records.
4. **Pipeline tests:** paging, fan-out, priorities, cancellation, checkpoints.
5. **Golden tests:** old and new final ceramics artifacts.
6. **PostgreSQL tests:** idempotency, partial updates, retirement, observations.
7. **Deployment smoke tests:** worker capability, control run, health, artifact.

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

- projection is deterministic and does not mutate neutral entities;
- pagination cursors cannot skip or duplicate pages during retry;
- optional budget exhaustion cannot stop discovery;
- connector cancellation leaves a resumable page-boundary checkpoint;
- an invalid checkpoint cannot corrupt a full run;
- one projector failure does not discard other dataset output;
- exact stock is never produced from an unverified order limit;
- partial refresh cannot erase fields owned by another dataset;
- secrets cannot enter entities, checkpoints, diagnostics, or artifacts.

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

URLs, entity IDs, product names, cursors, and exception bodies remain in bounded
job diagnostics rather than metric labels.

The operator view should answer:

- did enumeration complete;
- which datasets were produced;
- which optional fields were skipped and why;
- how much direct/browser/proxy traffic each priority used;
- whether the run resumed;
- whether the connector schema appears to have changed.

## 16. Rollout and compatibility policy

Every connector uses the same rollout ladder:

1. unit and replay tests;
2. local golden parity;
3. production shadow projection with no load;
4. source allowlist canary;
5. two successful scheduled cycles;
6. default new path with old rollback flag;
7. remove old path after a documented observation window.

During shadow mode, fetch once and run both projections over the same neutral
entity stream. Do not crawl the live source twice merely to compare code paths.

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
10. connector extensions treated as untrusted external data and size-bounded.

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

Risk: collecting neutral entities before projection duplicates large payloads.

Mitigation: page streams and immediate projection; never retain a whole source
unless a dataset explicitly requires it.

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

## 19. Implementation order by repository path

1. Add contracts under `catalogue-dump/src/mb_ceramics_catalogue/connectors/`
   and `datasets/` with unit tests.
2. Add a pipeline compatibility adapter that returns the existing
   `ScrapeResult` and summary shape.
3. Extract the ceramics projector while leaving `record.py` as its internal
   implementation.
4. Extend the registry to distinguish connectors from legacy scrapers.
5. Add capabilities and remove runner platform-name branching.
6. Add request priorities to `Fetcher` and proxy budget planning.
7. Implement Shopify connector, replay fixtures, checkpointing, and shadow
   comparison.
8. Roll Shopify out and retire only its legacy path.
9. Repeat for structured platforms.
10. Compose generic page discovery/parsers and migrate bespoke scrapers.
11. Add stock, price, and document datasets with storage migrations.
12. Migrate `sources.json` and control overlays to discriminated options.
13. Remove compatibility code and update documentation.

Do not combine more than one platform migration in a commit. A platform commit
must be revertible without reverting contracts already used by another one.

## 20. Completion checklist

- [ ] Neutral commerce and evidence contracts are versioned and documented.
- [ ] Connector and dataset protocols are stable.
- [ ] Ceramics projection is platform-independent.
- [ ] Shopify golden parity and production rollout are complete.
- [ ] WooCommerce golden parity and production rollout are complete.
- [ ] Remaining structured platform migrations are complete.
- [ ] Generic and bespoke scrapers emit neutral entities.
- [ ] Runner contains no scraper-name feature switches.
- [ ] Source configuration uses typed connector/dataset sections.
- [ ] Discovery, identity, detail, and optional budgets are enforced.
- [ ] Page-boundary checkpoints resume safely.
- [ ] Stock, price, and document datasets reuse one collection stream.
- [ ] Existing ceramics API output remains contract-compatible.
- [ ] All old paths and temporary rollback flags are removed.
- [ ] Operator documentation and contributor examples are current.
- [ ] Full unit, golden, PostgreSQL, service, and deployment smoke suites pass.

## 21. Definition of fully migrated

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
