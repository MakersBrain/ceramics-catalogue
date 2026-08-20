# Catalogue pipeline: from a hand-run script to a scheduled service

A plan to turn the ceramics catalogue collection into a stack that updates
prices daily and on demand, reports progress while it is happening, and can be
debugged from a browser rather than from a terminal that has to be attached
before the run starts.

Nothing inside `catalogue-dump/scrapers/` changes — the package moves, but not a
line of it is edited. The 4,700 lines of site-specific collection are the asset;
everything below is scaffolding around them.

---

## 1. Where it stands

| Piece | What it is | Lines |
|---|---|---|
| `catalogue-dump/dump.py` | asyncio crawl of the 80 sources in `sources.json`, writes one NDJSON per source plus `manifest.json` | 506 |
| `catalogue-dump/scrapers/` | 12 platform scrapers, shared fetcher, host limiter, response cache, record contract | 4,661 |
| `catalogue-dump/load_postgres.py` | separate step, shells out to `psql`, `\copy` into staging, `catalogue.load_record`, retire-unseen | 350 |
| `catalogue-dump/tui.py`, `interactive.py` | rich table and Textual app, both terminal-only | 535 |
| `catalogue-service/app.py` | read-only HTTP API over `catalogue.canonical_catalogue`, stdlib only | 200 |
| `catalogue-explorer/` | SvelteKit 5 browser: `/`, `/explore`, `/compare` | — |
| `docker-compose.yml` | Postgres 17 on 127.0.0.1:5434, the read API, a `loader` profile | — |

The collection itself is good. The operational surface around it is the gap.

### What is actually missing

1. **Nothing runs on a schedule.** A price update happens when a person types a
   command. "Daily" is currently a habit, not a property of the system.
2. **The scrape and the load are two manual steps** joined by files on disk.
   An operator has to notice the first finished and start the second.
3. **Progress is ephemeral.** `ProgressReporter` drives rich or Textual against
   the live `ScrapeResult` objects. When the process exits, all of it is gone.
   The only durable trace is `manifest.json`, written once, at the end.
4. **There is no per-source run history in the database.**
   `catalogue.import_runs` records the *load* — a status, a record count, two
   timestamps — and knows nothing about the crawl that produced it: no per-source
   rows, no timings, no error detail, no field coverage.
5. **A run can only be started from a shell** with the repository checked out,
   and only cancelled with Ctrl-C on that same terminal.
6. **No worker identity and no liveness.** Nothing can answer "is it running,
   and if not, when did it stop".
7. **The politeness limiter is per-process.** `HostLimiter` bounds concurrency
   inside one `dump.py`. Two processes double the request rate on every host,
   which is the one thing this crawler must not do.
8. **The loader needs `psql` on PATH** and builds SQL by string replacement
   (`RETIRE.replace("%(source)s", f"'{source}'")`, the run id interpolated into
   three statements). Source ids come from a checked-in config file so it is not
   an injection surface today, but it blocks running the load in-process and it
   gives no transaction control: a source is loaded, retired and committed
   across four separate `psql` invocations.
9. **Logs are unstructured and go to stderr.** Nothing is retained, nothing is
   searchable, and a failure discovered the next morning has left no evidence.
10. **No metrics of any kind**, and no tracing — so "which source is stuck and on
    what" is answerable only while a terminal is attached.
11. **It is not an installable package.** `[tool.uv] package = false`, no build
    backend, no entry points; `import scrapers` resolves only because Python puts
    the running script's directory on `sys.path`. A worker in an image cannot
    import any of this without games. See §4.
12. **One test file, and no lint, type or CI configuration** — despite
    `# noqa: BLE001` comments implying ruff was meant to run.
13. **The response cache defaults to a 7-day max age.** A daily run under
    `--cache-mode auto --cache-max-age 168` replays yesterday's pages and
    reports success while changing no prices. This alone would make a daily
    schedule a no-op — see §8.

---

## 2. Target shape

```
                    ┌──────────────────────────────────────────┐
                    │  catalogue-explorer  (SvelteKit)         │
                    │  /  /explore  /compare                   │
                    │  /ops  /ops/runs  /ops/sources           │
                    └───────┬──────────────────────┬───────────┘
                            │ SSE + JSON           │ SQL (read)
                            │ (proxied server-side)│
                    ┌───────▼──────────┐           │
                    │ catalogue-control│           │
                    │  POST /v1/runs   │           │
                    │  GET  /v1/events │           │
                    │  GET  /metrics   │           │
                    └───────┬──────────┘           │
                            │ enqueue / LISTEN     │
                    ┌───────▼──────────────────────▼───────────┐
                    │            PostgreSQL                    │
                    │  catalogue.*        (reference data)     │
                    │  catalogue.runs / jobs / job_progress    │
                    │  catalogue.job_events / workers / hosts  │
                    └───────▲──────────────────────▲───────────┘
                            │ claim / progress     │ read
                    ┌───────┴───────┐      ┌───────┴──────────┐
                    │ catalogue-    │ ...  │ catalogue-service│
                    │ worker  (xN)  │      │ read API, under  │
                    │               │      │ a generated spec │
                    └───────────────┘      └──────────────────┘

     Two OpenAPI documents, both generated and drift-checked in CI:
       catalogue.openapi.json      <- catalogue-service, for consumers
       catalogue-ops.openapi.json  <- catalogue-control, for operators
```

Five decisions carry the design.

**Postgres is the queue and the system of record.** It is already running, it is
already the place the catalogue lives, and the volume is trivial — 80 jobs a
day. `for update skip locked` gives multi-worker claiming; `listen`/`notify`
gives the UI live push without polling. No Redis, no broker, nothing new to
operate or back up.

**The worker is Python and imports `scrapers` directly.** Rewriting the
collection in another language to get a queue would be the tail wagging the dog.

**The write path is a new service, not an addition to `catalogue-service`.**
That module's docstring states the contract plainly: "there is no write path
here at all rather than a write path behind a permission." Keeping the read API
read-only, and putting run control in `catalogue-control`, preserves the
property rather than arguing with it.

**Worker conventions mirror `ateliera-app/apps/api/src/workers/lifecycle.ts`.**
Same event vocabulary (`worker.starting`, `worker.ready`, `worker.tick`,
`worker.retrying`, `worker.stopping`), same heartbeat-and-backoff shape, same
SSE approach as `apps/api/src/realtime/sse.ts`. An operator who knows one knows
the other.

**Both HTTP surfaces are described by a generated OpenAPI document**, following
the same never-hand-edit rule `ateliera-app` already applies to
`packages/api-contract`. This is what turns the read API from a docstring into a
contract, and it is the reason `catalogue-service` moves onto the same web base
as the control service — see §10.

---

## 3. Schema: `catalogue-dump/catalogue-ops-schema.sql`

New tables, all in the existing `catalogue` schema. Reference data is untouched.

```sql
-- A worker process. Rows are durable so a dead worker is visible as a stale
-- heartbeat rather than as an absence.
create table catalogue.workers (
  id                uuid primary key,
  hostname          text not null,
  pid               integer not null,
  version           text,
  capabilities      text[] not null default '{}',   -- e.g. {'browser'}
  started_at        timestamptz not null default now(),
  last_heartbeat_at timestamptz not null default now(),
  status            text not null,                  -- starting|idle|busy|paused|draining|stopped
  desired_state     text not null default 'running', -- running|paused|draining|stopping
  current_job_id    uuid
);

-- One collection run: every job it fanned out to shares this id.
create table catalogue.runs (
  id            uuid primary key default gen_random_uuid(),
  kind          text not null,          -- scheduled|manual|retry|backfill
  schedule_id   text,                   -- FK added after schedules is created
  scheduled_fire_at timestamptz,        -- identity of one cron occurrence
  requested_by  text,
  params        jsonb not null default '{}',  -- limit, cache mode, browser, delay
  status        text not null,          -- queued|running|complete|failed|cancelled
  created_at    timestamptz not null default now(),
  started_at    timestamptz,
  finished_at   timestamptz,
  summary       jsonb                   -- filled at the end: totals, per-source outcome
);

-- One source's share of a run. This is the queue.
create table catalogue.jobs (
  id                uuid primary key default gen_random_uuid(),
  run_id            uuid not null references catalogue.runs(id) on delete cascade,
  source_id         text not null,
  host              text not null,      -- politeness is per host, not per source
  state             text not null,      -- queued|leased|running|paused|succeeded|failed|cancelled|skipped
  priority          smallint not null default 100,
  attempt           smallint not null default 0,
  max_attempts      smallint not null default 3,
  requires          text[] not null default '{}',   -- capabilities the worker must have
  scheduled_for     timestamptz not null default now(),
  lease_owner       uuid references catalogue.workers(id),
  lease_expires_at  timestamptz,
  cancel_requested  boolean not null default false,
  pause_requested   boolean not null default false,
  resume_without_attempt boolean not null default false,
  started_at        timestamptz,
  finished_at       timestamptz,
  error             text,
  trace_id          text,               -- 32 lower-case hex characters when tracing is active
  artifact_path     text,               -- run/job-namespaced NDJSON audit artifact
  artifact_sha256   text,
  artifact_size     bigint,
  summary           jsonb,              -- the existing run_source() summary, verbatim
  unique (run_id, source_id)
);
create index on catalogue.jobs (state, scheduled_for, priority)
  where state in ('queued', 'leased', 'running');
create index on catalogue.jobs (lease_expires_at)
  where state in ('leased', 'running');

-- Live counters for a running job. One row per job, updated in place: the UI
-- reads this and nothing else for the progress bars.
create table catalogue.job_progress (
  job_id         uuid primary key references catalogue.jobs(id) on delete cascade,
  updated_at     timestamptz not null default now(),
  phase          text,                  -- discovering|fetching|parsing|paused|loading
  discovered     integer not null default 0,
  records        integer not null default 0,
  requests       integer not null default 0,
  rendered_pages integer not null default 0,
  error_count    integer not null default 0,
  truncated      boolean not null default false,
  in_flight      jsonb not null default '[]'   -- from scrapers.activity, capped
);

-- Append-only log stream, per job. Pruned on a retention window.
create table catalogue.job_events (
  id       bigint generated always as identity primary key,
  job_id   uuid not null references catalogue.jobs(id) on delete cascade,
  at       timestamptz not null default now(),
  level    text not null,               -- debug|info|warning|error
  event    text,                        -- structured name, e.g. 'job.leased'
  message  text not null,
  data     jsonb
);
create index on catalogue.job_events (job_id, id);

-- Cross-worker politeness. A host is crawled only up to its configured bound.
create table catalogue.hosts (
  host            text primary key,
  max_concurrency smallint not null default 1 check (max_concurrency > 0),
  delay_seconds   numeric
);

-- One row per usable concurrency slot. Changing max_concurrency reconciles
-- these rows while holding the host row lock; occupied slots are never removed.
create table catalogue.host_leases (
  host         text not null references catalogue.hosts(host) on delete cascade,
  slot         smallint not null check (slot > 0),
  leased_by    uuid references catalogue.workers(id),
  job_id       uuid references catalogue.jobs(id),
  leased_until timestamptz,
  primary key (host, slot)
);

-- What should run, and when.
create table catalogue.schedules (
  id            text primary key,
  enabled       boolean not null default true,
  cron          text not null,           -- '0 3 * * *'
  timezone      text not null default 'Europe/Paris',
  source_filter jsonb not null default '{"all": true}',
  params        jsonb not null default '{}',
  last_fired_at timestamptz,
  next_fire_at  timestamptz
);

alter table catalogue.runs
  add constraint runs_schedule_id_fkey
  foreign key (schedule_id) references catalogue.schedules(id);
create unique index runs_scheduled_occurrence_key
  on catalogue.runs (schedule_id, scheduled_fire_at)
  where schedule_id is not null;

-- Operator-owned overrides. The checked-in sources.json remains the definition
-- of scraper structure; this table is the mutable operational layer over it.
create table catalogue.source_settings (
  source_id          text primary key,
  enabled            boolean not null default true,
  paused             boolean not null default false,
  schedule_id        text references catalogue.schedules(id),
  params             jsonb not null default '{}',
  updated_at         timestamptz not null default now(),
  updated_by         text
);

-- The ordering authority for everything on the live stream. One bigint
-- sequence across every kind of event, so a browser reconnecting with
-- Last-Event-ID resumes from an exact position rather than approximately.
create table catalogue.event_log (
  id       bigint generated always as identity primary key,
  at       timestamptz not null default now(),
  topic    text not null,        -- run|job|worker|notification|schedule
  type     text not null,        -- job.failed, worker.stopped, run.complete, ...
  run_id   uuid,
  job_id   uuid,
  worker_id uuid,
  source_id text,
  payload  jsonb not null default '{}'
);
create index on catalogue.event_log (id) include (topic);
create index on catalogue.event_log (run_id, id) where run_id is not null;

-- Things an operator should be told about whether or not a browser was open.
-- Durable and acknowledgeable, because the whole point is surviving the night.
create table catalogue.notifications (
  id              bigint generated always as identity primary key,
  at              timestamptz not null default now(),
  severity        text not null,          -- info|warning|critical
  kind            text not null,          -- job.failed, source.stale, worker.lost, ...
  title           text not null,
  body            text,
  run_id          uuid, job_id uuid, source_id text, worker_id uuid,
  -- Three retries of one source are one notification, not three.
  dedup_key       text not null,
  -- A condition that can end: 'source.stale' clears when the source succeeds.
  resolved_at     timestamptz,
  acknowledged_at timestamptz,
  acknowledged_by text
);
create unique index on catalogue.notifications (dedup_key)
  where resolved_at is null and acknowledged_at is null;
```

`catalogue.import_runs` gains `run_id uuid references catalogue.runs(id)`, so a
load is traceable to the crawl that produced it. Nothing else about it changes —
`catalogue.load_record` still references it.

### 3.1 Edges go in the log, levels do not

The distinction that keeps the live stream affordable:

- **Edges** — a job failed, a worker stopped, a run completed, a notification
  was raised. Discrete, meaningful once, and *bad to miss*. These are inserted
  into `catalogue.event_log`, which is where their ordering and their
  replayability come from.
- **Levels** — a job's counters, a worker's heartbeat. Current values that are
  meaningful only as the latest reading. These live in `job_progress` and
  `workers`, updated in place, and are **never** written to the event log.

Without that split the log is unusable. A three-hour run of 80 sources emitting
progress at 1 Hz is roughly 860,000 rows per run, all of them stale within a
second of being written. And there is nothing to replay: a client that missed
forty progress readings does not want them, it wants the current one.

A trigger on `event_log` insert issues `notify catalogue_ops, '<id>'` — **the id
only, never the row**. Two reasons, both of which have bitten this pattern
before:

1. `notify` payloads are capped at 8000 bytes. A payload carrying
   `job_progress.in_flight` would exceed it and the insert would fail.
2. `notify` is fire-and-forget. A listener that is reconnecting when one fires
   loses it permanently. Carrying the id means the notification is a hint to go
   read, and the table stays the authority — so a missed hint costs latency,
   never data.

`job_progress` uses a separate level notification. Its throttled upsert issues
`notify catalogue_progress, '<job-id>'`; the control service then reads that
job's current row. The payload is still only an identifier, never `in_flight`.
Progress is disposable and is re-snapshotted on stream bootstrap, so it does not
need an event-log id or replay history.

Both listeners maintain a watermark. After LISTEN is established, and every
five seconds thereafter, the control service queries `event_log` for ids above
the last dispatched id before waiting for more hints. Re-establishing LISTEN
does the same catch-up query. Consequently a lost notification delays an edge
by at most one catch-up interval even if it was the last event and every browser
connection remained open; LISTEN/NOTIFY is never being mistaken for the queue.

**Write amplification.** A source doing 3,000 requests must not do 3,000
progress updates. The worker's Postgres sink throttles to at most one write per
second per job, and coalesces — the counters are cumulative, so a dropped
intermediate value costs nothing. `job_events` is written per event but at
`info` and above by default; `debug` goes to stdout only unless the job was
started with `log_level=debug`.

**Retention.** `event_log` is pruned at 30 days beside `job_events`. Resolved
and acknowledged notifications at 90 days; unresolved ones are kept, because an
unresolved notification is the thing nobody dealt with.

---

## 4. Package and code architecture

Everything above assumes the collection code can be imported by a long-lived
worker in a container. Today it cannot — not really — and the reasons are worth
fixing on their own merits, not just to unblock the worker.

### 4.1 It is not a package

`pyproject.toml` says `[tool.uv] package = false`. There is no build backend, no
`[project.scripts]`, and no dev dependency group. `catalogue-dump/` is a folder
of top-level modules — `dump.py`, `tui.py`, `interactive.py`, `load_postgres.py`,
`probe.py`, `adapters.py` — beside one real package, `scrapers/`.

So `dump.py` does `import scrapers`, `import tui`, `import interactive`, and
those resolve only because Python puts the *script's own directory* on
`sys.path`. It works when you run `python3 dump.py` from that folder. The compose
file already shows the strain: it runs
`python3 /work/catalogue-dump/load_postgres.py` against a read-only bind mount,
and `tests/test_scrapers.py` opens with `import scrapers` for the same reason.

That is fine for a script and wrong for a service. A worker installed into an
image has no such directory, cannot be `pip install`ed, has no version to report
in `catalogue.workers.version`, and cannot be imported by anything else without
`sys.path` games.

Smaller symptoms of the same thing: `dump.py` defines `interactive_available()`
at line 34, in the middle of its import block, with four more imports after it.
`ProgressReporter.open()` creates `self.saved_handlers` and `self.saved_httpx`,
which `close()` then reads through `getattr(self, …, default)` because they may
not exist. There are `# noqa: BLE001` comments throughout, so somebody intended
ruff to run — but no ruff configuration exists anywhere in the repository.

### 4.2 Target layout

A real `src`-layout package, so an installed build is what gets imported and the
working directory never silently satisfies an import:

```
catalogue-dump/
  pyproject.toml                    # hatchling backend, [project.scripts], dev group
  src/mb_ceramics_catalogue/
    __init__.py                     # __version__, nothing else
    cli/
      dump.py       worker.py      load.py       probe.py
    config/
      sources.py                    # SourceConfig / SourcesFile  (pydantic)
      settings.py                   # Settings                    (pydantic-settings)
    crawl/
      runner.py                     # CrawlRunner — orchestration, nothing else
      session.py                    # async CM building Fetcher/limiter/browser/cache
      progress.py                   # ProgressSink protocol + implementations
      artifacts.py                  # write_source / write_partial / manifest
    scrapers/                       # moved verbatim; not one line changes
    storage/
      postgres.py                   # load_source(), from load_postgres.py
      history.py                    # the SQLite path, out of the orchestrator
      schema/*.sql
    ops/
      queue.py  leases.py  events.py  notifications.py  worker.py  schedule.py
    observability/
      logging.py  tracing.py  metrics.py
    ui/
      dashboard.py                  # was tui.py
      interactive.py
  tests/
```

`[project.scripts]` gives `catalogue-dump`, `catalogue-worker` and
`catalogue-load` as real entry points, which is what removes
`python3 /work/catalogue-dump/…` from the compose file and the Quadlet units.

The `scrapers/` package moves without edits. It is already the best-structured
part of the codebase — a lazy `REGISTRY` mapping names to `"module:class"`, an
ABC, a shared `Fetcher` taking its collaborators by constructor injection. The
refactor is bringing everything else up to it, not changing it.

### 4.3 Decomposing `dump.py`

`main()` is 164 lines and does at least eight things: parse arguments, configure
logging, load and validate `sources.json`, create the output directory, build the
manifest, construct the progress display, build client/limiter/browser/cache/
fetcher, run and cancel tasks, collect results, persist SQLite history, print a
report, and write the manifest.

Separately, `persist_history()` is 84 lines of SQLite DDL and upsert logic living
inside the crawl orchestrator — a whole storage backend inlined into the module
that is supposed to be deciding what to crawl.

The split:

| New home | Takes | Why |
|---|---|---|
| `cli/dump.py` | argparse, then hand off | An entry point should parse and delegate. Nothing else. |
| `config/settings.py` | every option, as one validated object | Options arrive from a CLI *and* from a job's `params` jsonb. One model, two sources. |
| `crawl/session.py` | client, limiter, browser, cache, fetcher | An `async with` that guarantees the browser closes — today that is a bare `finally` in `main()`. |
| `crawl/runner.py` | the task orchestration and cancellation | The actual subject of the module, currently ~40 lines buried in 164. |
| `crawl/artifacts.py` | `write_source`, `write_partial`, manifest | Already good functions, just misplaced. |
| `storage/history.py` | `persist_history` | A storage backend belongs in storage. |
| `crawl/progress.py` | the `ProgressSink` split from §5.2 | Display selection stops being the reporter's job. |

`run_source()` survives nearly intact — it is already the right shape, and the
worker calls it directly.

### 4.4 Typed configuration

`sources.json` is 80 entries with roughly fifteen optional keys, read everywhere
as `config.get("…")` against `dict[str, Any]`. A typo in a key is silent: write
`store_category` instead of `store_categories` and the source quietly crawls its
entire catalogue instead of the allowlist, and nothing says so.

Pydantic models — `SourceConfig`, `SourcesFile` — make that structural. Validated
at load, so a bad key is an error naming the source and the field. `scraper` can
validate against `scrapers.REGISTRY`, and `vat_status` against its literal set;
the existing test asserts a couple of these by hand and can then delete those
assertions.

The same models generate the run-parameter schema for `POST /v1/runs`, so the
CLI, the API and the scheduler agree on what a valid run is by construction
rather than by three separate `.get()` calls.

**One global to remove while doing this.** `scrapers.record.learn_sources(config)`
mutates module-level state, which is acceptable in a process that crawls once and
exits and wrong in a worker that handles thousands of jobs. It becomes a
`RecordBuilder(sources)` instance passed to the scraper, alongside the existing
constructor injection they already use.

### 4.5 Async

The current code is competent asyncio — `Semaphore` gating, per-host locks, a
`ContextVar` for source attribution, `CancelledError` handled with intent. Four
things it should adopt, all available on the `>=3.11` it already requires:

- **`asyncio.TaskGroup` instead of `gather` plus a manual cancel loop.** Today a
  fatal error in the orchestration leaves the other 79 tasks to be cancelled by
  hand in an `except` clause. A TaskGroup makes that structural. Per-source
  handles are still kept, because cancelling *one* job is a requirement (§5.6),
  and `tg.create_task()` returns them.
- **`asyncio.timeout()` per source.** There is no per-source deadline anywhere
  today. A source that hangs — a slow origin, a browser that never settles —
  blocks its slot indefinitely, and in a scheduled daily run that means the
  03:00 run is still going at 09:00 with nobody watching. A generous default
  with a per-source override in `SourceConfig`.
- **A real SIGTERM handler.** `main()` catches `KeyboardInterrupt`, which is what
  Ctrl-C raises. Containers and systemd send **SIGTERM**, which is not
  `KeyboardInterrupt` and is not caught — so `docker stop` today can land in the
  middle of `write_source`. `loop.add_signal_handler(SIGTERM, …)` driving the
  same graceful path the stop button uses.
- **Replace `await asyncio.sleep(0.2)`** — the "Textual needs a moment to take
  the terminal" wait in `ProgressReporter.open()` — with an `asyncio.Event` the
  app sets when mounted. A sleep standing in for a synchronisation primitive is
  a race that passes on a fast machine.

Optional, and worth measuring first: records are accumulated in
`ScrapeResult.records` and written once at the end, so a source's entire
catalogue is resident. At 47,000 records across all sources that is comfortable.
If it ever is not, streaming each record to the NDJSON as it is built and
`COPY`ing from the file bounds it — but there is no reason to complicate the code
before the numbers ask for it.

### 4.6 Logging

Two places perform surgery on the root logger. `ProgressReporter.open()` does
`logging.getLogger().handlers = [handler]` and restores from an instance
attribute; `tui.Dashboard.capture_logging()` does the same with a `RichHandler`.
Both also reach in and change the `httpx` logger's level. It works, and it is the
display layer reconfiguring global logging as a side effect of being constructed.

That has to go regardless of style, because a worker must keep emitting
structured JSON to stdout the whole time — its logs are how you debug it — and
today constructing a display would silently replace the handler that does.

- **structlog over the stdlib**, configured **once**, in
  `observability/logging.py`, called only from an entry point. JSON renderer when
  stdout is not a TTY, console renderer when it is.
- **Displays add a handler, never replace the handler list.** `LogRelay` becomes
  an additional sink. The console handler is quietened by the display through a
  documented switch rather than by having its list swapped underneath it.
- **Context comes from contextvars, not from format strings.**
  `structlog.contextvars.bind_contextvars(run_id=…, job_id=…, source=…,
  worker_id=…)` — which fits the existing `CURRENT_SOURCE` pattern exactly, and
  means every line inside a source's task carries its source without a single
  call site passing it. Today `LOGGER.info("started source=%s", source)` hand-
  formats what a contextvar already knows.
- **Event names, not sentences.** `log.info("job.started", source=…, scraper=…)`,
  matching the vocabulary in §9 and the `job_events` rows. One vocabulary across
  stdout, the database and the SSE stream.

### 4.7 Tracing

A run is 80 concurrent sources issuing thousands of requests, and the question
during a bad one is always "what is that source waiting on". `scrapers/activity.py`
answers it for *live* requests and keeps 40 of them; nothing answers it
afterwards.

OpenTelemetry, with a span hierarchy that mirrors the domain: `run` → `job`
(one per source) → `http.request`. `opentelemetry-instrumentation-httpx`
produces the request spans without touching `Fetcher`, carrying URL, status and
duration; `Fetcher`'s own decisions — cache hit, robots denial, browser
fallback, backoff — become span attributes and events, and they are exactly the
things that are currently invisible.

Being honest about scope: **there is no collector to send traces to** (§9 —
nothing in `makersbrain-infra` runs one). So the SDK is wired with the exporter
defaulting to off, enabled by `OTEL_EXPORTER_OTLP_ENDPOINT` alone. What ships
now is the `trace_id` stamped onto `catalogue.jobs` and included in every log
line, so the correlation exists in the data from day one and a collector added
later reads history rather than starting from zero. The instrumentation is
cheap; the retrofit would not be.

### 4.8 Metrics

Defined once in `observability/metrics.py` as OpenTelemetry instruments with a
Prometheus exporter, feeding the `/metrics` endpoint §9 already specifies.

The instruments the crawl should carry, none of which exist today:

| Instrument | Kind | Answers |
|---|---|---|
| `catalogue_requests_total{source,host,outcome}` | counter | which host is failing |
| `catalogue_request_duration_seconds{host}` | histogram | which host is slow |
| `catalogue_cache_total{outcome}` | counter | is a "fresh" run actually replaying (§8) |
| `catalogue_records_total{source}` | counter | did a source shrink (§6.6) |
| `catalogue_browser_renders_total{source}` | counter | what the expensive path costs |
| `catalogue_host_backoff_seconds{host}` | histogram | are we being throttled |
| `catalogue_parse_failures_total{source,field}` | counter | which extractor is drifting |

`HostLimiter` already computes the backoff and concurrency decisions these
describe — the numbers exist and are simply thrown away.

### 4.9 Tests, linting, types

Currently: one `unittest` file, no configuration for ruff, mypy, or pytest, and
no CI. To refactor safely, that order has to reverse — the tests come first.

- **pytest + `pytest-asyncio`**, keeping the existing `unittest` classes, which
  pytest runs unchanged.
- **The response cache is already a fixture corpus.** `--cache-mode replay`
  never touches the network and replays recorded responses; that is a recorded
  integration test suite that exists but is not used as one. Checking in a small
  cache directory per platform scraper gives real end-to-end tests for all
  twelve, offline and deterministic.
- **Characterization tests before any of the moves in §4.3.** Run each scraper in
  replay mode, freeze the NDJSON as a golden file, and assert byte-equality
  through the refactor. Without that, this is a large mechanical change to
  untested code and it will silently alter output.
- **ruff** with the rules the `# noqa: BLE001` comments already imply somebody
  wanted, and **mypy** in strict mode on new modules, non-strict on `scrapers/`
  initially so the refactor is not held hostage to typing 4,700 lines.
- **CI** running lint, types, tests and the OpenAPI drift check from §10.

### 4.10 Sequencing, honestly

This is a large change to a codebase with one test file, and it produces no user-
visible improvement on its own. Two things keep it from being reckless:

1. **It goes first, in phase 1**, before the worker exists. Refactoring the
   orchestrator while a worker is being built on top of it is the worst order.
2. **Golden-file tests come before the first move**, not after (§4.9). They are
   most of the risk mitigation and they are cheap, because replay mode already
   does the hard part.

What is explicitly *not* in scope: touching the scrapers, changing the record
contract, or altering the NDJSON output. The refactor is a no-op by design, and
the golden files are how that claim gets checked rather than asserted.

---

## 5. The worker

New: `ops/worker.py` in the package from §4, alongside `ops/queue.py`,
`ops/leases.py`, `ops/events.py` and `ops/schedule.py`. It lives in the same
distribution as the crawl because it imports `crawl.runner` and `scrapers`
directly — and after §4.1 that is a real import rather than a `sys.path`
coincidence.

### 5.1 The loop

```
register in catalogue.workers  ->  heartbeat every 5s in a background task
loop:
  observe desired_state; claim only while it is running
  finalise any expired leases that have exhausted max_attempts
  claim a queued or expired job   (skip locked, honouring capabilities)
  acquire a host slot       (else release with a short backoff, no attempt burnt)
  mark running and consume the attempt
  run the scrape           (existing run_source, with the Postgres sink)
  load it into Postgres    (in-process, one transaction — §5.3)
  release lease, mark succeeded/failed, write summary
on SIGTERM: behave like drain, bounded by the shutdown grace period, then requeue
the current job if it has not finished; mark stopped
```

Claiming is one statement, but deliberately does **not** consume an attempt. An
attempt begins only after the host slot has been acquired and the job changes to
`running`; host contention and a crash between claim and start are not scraper
attempts.

```sql
with candidate as (
  select j.id
    from catalogue.jobs j
    left join catalogue.source_settings s on s.source_id = j.source_id
   where (attempt < max_attempts or resume_without_attempt)
     and requires <@ %(capabilities)s::text[]
     and coalesce(s.enabled, true)
     and not coalesce(s.paused, false)
     and (
       (state = 'queued' and scheduled_for <= now())
       or (state in ('leased', 'running') and lease_expires_at < now())
     )
   order by priority, scheduled_for
   for update of j skip locked
   limit 1
)
update catalogue.jobs j
   set state = 'leased', lease_owner = %(worker)s,
       lease_expires_at = now() + interval '5 minutes'
  from candidate
 where j.id = candidate.id
returning j.*;
```

After acquiring a host slot, one conditional update changes `leased → running`
and increments `attempt` unless `resume_without_attempt` is set, then clears
that flag; it succeeds only when the same worker still owns the unexpired job
lease. The lease is renewed on every heartbeat while the job runs. A durable
operator resume sets the flag, while crash retries do not, so human pauses cannot
exhaust the failure budget.

At the start of every tick, the worker also atomically changes expired
`leased|running` jobs with `attempt >= max_attempts` and no operator-resume flag
to `failed`, clears their
owners, releases any expired host slots, and emits the terminal edge. Jobs below
the limit are selected by the query above directly; they do not need a separate
reaper transition through `queued`. Thus a dead worker is recovered, retries
are bounded, and no expired terminal job can remain stranded forever.

When any job enters a terminal state, the same transaction locks its parent run
and, if no non-terminal sibling remains, computes the summary and changes the
run to `complete`, `failed`, or `cancelled`. This locked aggregation is the
authority for `run.complete`/`run.degraded`, avoiding finishing workers racing
to publish different summaries.

### 5.2 Progress without touching the scrapers

`ProgressReporter` in `dump.py` currently does three jobs: it decides which
display to use, it holds state, and it renders. §4.3 moves it to
`crawl/progress.py`; the split that matters here is pulling the third out into a
sink:

```python
class ProgressSink(Protocol):
    async def started(self, source: str, result: ScrapeResult) -> None: ...
    async def progress(self, source: str, result: ScrapeResult) -> None: ...
    async def finished(self, source: str, summary: dict) -> None: ...
    def log(self, record: logging.LogRecord) -> None: ...
```

- `TerminalSink` wraps the existing rich and Textual paths verbatim. The CLI on a
  terminal behaves exactly as it does today.
- `PostgresSink` writes `job_progress`, appends to `job_events` (via a
  `logging.Handler`, the same trick `interactive.LogRelay` already uses), and
  lets the throttled progress upsert notify `catalogue_progress` with the job id.
  Durable edges are separately inserted into `event_log`, whose trigger notifies
  `catalogue_ops` with the event id.

Sinks are additive — a worker run with a TTY attached can have both. That is only
true once §4.6 stops the display from swapping the root logger's handler list out
from under everything else.

Because both read the live `ScrapeResult` objects the scrapers already append
to, no scraper changes and nothing is reported twice. A ticker task samples at
1 Hz rather than the scrapers pushing.

`scrapers.activity.ACTIVITY` gives the in-flight request list per source for
free — it is what the Textual view already shows. The Postgres sink writes the
top 10 into `job_progress.in_flight`, so the browser gets the same view the
terminal has.

### 5.3 Loading in-process

`load_postgres.py` becomes a library plus a CLI wrapper:

```python
def load_source(conn, source: str, records: Iterable[dict],
                *, whole: bool, run_id: UUID) -> LoadReport
```

- psycopg 3 `cursor.copy()` into `catalogue.import_staging` instead of `\copy`,
  so no `psql` binary and no subprocess.
- Real parameters everywhere — the run id and source name stop being string
  replacements.
- One transaction per source covering stage, `load_record`, retire-unseen and
  truncate. Today a crash between the four `psql` calls leaves staging populated
  and retirement half-applied.
- The `plan_load` / `authoritative` logic is preserved exactly. Its rules are
  the load's safety property and they must survive the move: an empty file is
  never grounds for retirement, a partial file only ever adds, a truncated run
  is adds-only.
- Staging becomes a temp table per connection rather than a shared unlogged
  table, so two workers cannot collide in it. This is required by concurrency,
  not optional.

The existing CLI (`docker compose run --rm loader`) keeps working as a thin
wrapper, for backfills and for loading an old dump directory.

The worker writes the NDJSON artifact as well as loading it. Artifacts live at
`dumps/<run-id>/<job-id>/<source>.ndjson` (or `.partial.ndjson`), never at a
shared source-only name. After an atomic file rename the worker records the
relative path, SHA-256 and byte size on `catalogue.jobs`. The file is therefore
a per-run audit trail; `write_source`'s never-replace-a-good-file-with-an-empty-
one rule stays intact within that run namespace.

### 5.4 Politeness across workers

This is the one genuinely new correctness problem. `HostLimiter` bounds
concurrency inside a process; three worker containers would triple the load on
every shop.

`catalogue.hosts` plus `catalogue.host_leases` is the cross-process bound. Before
running, a worker locks the host row and conditionally claims one free or expired
slot numbered `1..max_concurrency`; if it cannot, the job is released back to
`queued` with `scheduled_for = now() + 30s` and **no attempt consumed**. The
slot lease is renewed with the job heartbeat and released on completion. A
configuration update creates missing slots before increasing the limit and does
not remove a slot above a reduced limit until its current lease has ended.

Consequence worth stating: jobs are per source, and several sources can share a
host only where a manufacturer and its shop are the same site. Default
`max_concurrency` of 1 per host is right, and per-host overrides live in the
table where they can be tuned without a deploy.

### 5.5 Browser jobs

`ceramicolours` and `keramik-kraft` need camoufox. A browser makes the image
large and the process memory-hungry, and most workers do not need one. Jobs for
those sources carry `requires = {'browser'}`; only workers started with
`--capabilities browser` claim them. A small browser-capable pool and a larger
plain pool, from two images.

### 5.6 Pause, resume, cancellation and worker control

There are deliberately separate controls because “pause”, “stop this source”
and “stop this worker” have different safety properties:

- **Pause a source job.** Set `pause_requested`. The shared `Fetcher` checks an
  async gate before each request, so no scraper code changes. In-flight requests
  may finish, then the worker changes the job to `paused`, closes the gate,
  releases its host slot and keeps renewing its job lease. Resume while that
  owner is healthy reacquires a host slot and opens the gate, continuing the
  in-memory scraper session. To avoid retaining browser/process state forever,
  a configurable pause timeout (30 minutes by default) writes the partial
  artifact and clears the owner/lease while leaving the job `paused`; resuming
  that durable state requeues and restarts the source from the beginning. An
  operator pause never consumes an additional retry attempt.
- **Stop a source job.** Set `cancel_requested`. The worker sees it on the next
  heartbeat and cancels the asyncio task. The existing `partial_result` /
  `write_partial` path keeps what was collected in the job's artifact namespace,
  `plan_load` refuses to retire against a partial, and the job becomes terminal
  `cancelled`. Resume is not offered for a cancelled job; the operator explicitly
  retries it as a new attempt or includes it in another run.
- **Pause a worker.** Set its `desired_state=paused`. It stops claiming and
  finishes its current source, then remains alive and heartbeating as `paused`.
  `desired_state=running` resumes claiming. This controls the registered process,
  not the deployment replica count.
- **Drain or stop a worker.** `draining` stops new claims, finishes the current
  source and exits. `stopping` cancels the current source through the same safe
  partial-artifact path and exits. A systemd/Compose restart policy may create a
  new worker afterward, so persistently removing capacity is a deployment scale
  operation, not something the database control API pretends to guarantee.

All transitions are conditional on the current state and lease owner, write an
edge to `event_log`, and are idempotent when the same button is pressed twice.
Pausing a source in `source_settings` also sets `pause_requested` on its active
jobs; resuming the source removes the claim gate but does not automatically
resume individually paused jobs, so a broad administrative toggle cannot
silently restart abandoned work.

---

## 6. `catalogue-control`

A new small service. Starlette + uvicorn + psycopg 3 async.

The stdlib-only argument that holds for `catalogue-service` does not hold here:
SSE over a `listen` connection with per-subscriber fan-out is materially more
than a request/response loop, and hand-rolling it on `ThreadingHTTPServer` would
be more code to audit, not less.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/runs` | create a run and its jobs; `{sources, params, requested_by}` → 202 |
| `GET` | `/v1/runs` | run history, paged |
| `GET` | `/v1/runs/{id}` | one run with its jobs and their progress |
| `POST` | `/v1/runs/{id}/cancel` | set `cancel_requested` on every unfinished job |
| `POST` | `/v1/jobs/{id}/pause` | pause at the next fetch boundary |
| `POST` | `/v1/jobs/{id}/resume` | resume in place or requeue a durably paused job |
| `POST` | `/v1/jobs/{id}/retry` | requeue one source |
| `GET` | `/v1/jobs/{id}/logs?after=` | log tail, cursor-paged on `job_events.id` |
| `GET` | `/v1/workers` | worker roster with heartbeat age |
| `POST` | `/v1/workers/{id}/{action}` | `pause`, `resume`, `drain`, or `stop` |
| `GET` | `/v1/sources` | `sources.json` joined to last outcome per source |
| `PUT` | `/v1/sources/{id}` | enable/disable, pause/resume, schedule and parameter overrides |
| `GET`/`PUT` | `/v1/schedules` | list and edit schedules |
| `GET` | `/v1/notifications` | durable notifications, filterable on unacknowledged |
| `POST` | `/v1/notifications/{id}/ack` | acknowledge one |
| `GET` | **`/v1/events`** | **SSE: workers, runs, jobs, progress, notifications** |
| `GET` | `/metrics` | Prometheus text |
| `GET` | `/health` | liveness |

Every `/v1/*` endpoint, including SSE, run history and logs, requires a bearer
token from `CATALOGUE_CONTROL_TOKEN`; only `/health` and `/metrics` are exempt.
The service is not published on the host, which is defence in depth rather than
its authentication boundary. The SvelteKit server routes authenticate the user
session before proxying any JSON or SSE request and attach the control token
server-side. Read-only operator roles may be added later without changing this
default-deny boundary.

### 6.1 One stream, topic-filtered

```
GET /v1/events?topics=workers,notifications
GET /v1/events?topics=jobs,progress&run_id=<uuid>
```

**One endpoint, not one per concern.** A browser on `/ops` wants workers and
notifications; a browser on `/ops/runs/[id]` wants those *plus* that run's jobs
and progress. Separate endpoints mean three `EventSource` objects per tab, and
HTTP/1.1 caps a browser at roughly six connections per origin — two tabs open
and the app deadlocks against its own streams. One multiplexed connection,
filtered by `topics` and optionally narrowed by `run_id`, avoids the whole
problem.

Topics: `workers`, `runs`, `jobs`, `progress`, `notifications`, `schedules`.
Omitting `topics` subscribes to everything except `progress`, which is the
expensive one and should be asked for deliberately.

Each message uses the SSE `event:` field so a client writes
`stream.addEventListener('worker.changed', …)` rather than parsing a
discriminator out of every payload:

```
id: 40213
event: worker.changed
data: {"worker_id":"…","status":"busy","current_job_id":"…","source":"ceradel"}

event: job.progress
data: {"job_id":"…","source":"ceradel","records":812,"requests":1104,
       "phase":"fetching","in_flight":[…],"at":"2026-08-08T09:14:22Z"}

id: 40214
event: notification.raised
data: {"id":991,"severity":"warning","kind":"source.stale",
       "title":"keramik-kraft returned no records for 3 runs","source_id":"keramik-kraft"}
```

Note which messages carry an `id` and which do not — that is §3.1 on the wire.
Edges are numbered and replayable; `job.progress` is a level, so it is
deliberately unnumbered and never replayed.

### 6.2 Live worker updates

Two mechanisms, because "worker status" is two different questions:

- **Status changes are edges.** A worker going `idle → busy`, taking a job,
  draining on SIGTERM or stopping writes a row to `event_log`, and subscribers
  get `worker.changed` immediately.
- **Liveness is a level, and it is a clock computation.** A worker heartbeating
  every 5s must not emit an event every 5s — and it does not need to. The stream
  sends a `worker.roster` snapshot every 5s carrying each worker's
  `last_heartbeat_at`, and **the browser derives the age locally**. A worker
  that has silently died therefore shows as stale within one tick *without any
  event ever arriving*, which is precisely the case where events cannot help:
  nothing fires when a process stops existing.

The control service raises a `worker.lost` notification when a heartbeat exceeds
three intervals, so the failure is durable and not merely visible to whoever
happened to be looking.

### 6.3 Reconnection, and why the buffer is only a cache

Following `apps/api/src/realtime/bootstrap-buffer.ts` and `realtime/sse.ts`:

- Each control process holds **one** `listen` connection and keeps the last
  ~1,000 events in memory.
- Each process tracks the greatest event id it has dispatched. Notifications
  wake a query for all rows above that watermark; a five-second reconciliation
  query and the post-reconnect query perform the same read. This closes the
  otherwise permanent hole where the final notification was lost while clients
  remained connected.
- A client reconnects with `Last-Event-ID`. If that id is inside the buffer, it
  is replayed from memory. If it is older, the service reads the gap straight
  from `catalogue.event_log` — which is why the durable log exists. If the gap
  is larger than a cap, the service sends `event: resync` and the client
  refetches state over the normal JSON endpoints instead of replaying thousands
  of rows.
- The stream **opens** with a `bootstrap` event carrying the current worker
  roster, active run, and unacknowledged notifications, so a client never has to
  make a second request to render its first frame. Progress subscribers also get
  the current `job_progress` snapshot for every live job.

The in-memory buffer is a latency optimisation over the table, never the source
of truth. That is what lets `catalogue-control` run more than one replica: ids
come from a single Postgres sequence, so a client that reconnects to a
*different* replica still resumes correctly — worst case its buffer misses and
the replica reads the rows.

### 6.4 Backpressure and coalescing

An 80-source run emitting progress at 1 Hz is 80 messages a second to every
connected browser, to render a table that changes visibly perhaps twice a
second. So:

- **Progress is coalesced per subscriber** in a 500 ms window, keyed on job id,
  latest wins. Lossless by construction: the counters are cumulative, so a
  dropped intermediate reading is a reading nobody would have seen anyway.
- **Per-subscriber queues are bounded.** On overflow, progress messages are
  dropped first, since they are levels. If durable events would be lost, the
  subscriber gets `resync` and is disconnected rather than being silently served
  an incomplete history.
- **15 s keepalive comments**, plus `X-Accel-Buffering: no` and a disabled proxy
  response buffer. Caddy needs `flush_interval -1` on the `reverse_proxy` for
  the stream to arrive as it is written rather than in blocks.

### 6.5 Authentication

`EventSource` cannot set request headers, so a bearer token cannot be attached
by the browser. The explorer sidesteps this entirely: the browser connects to a
SvelteKit `+server.ts` route authenticated by the session cookie, and SvelteKit
opens the upstream stream with the token server-side. The token never reaches
the browser and `catalogue-control` stays unpublished.

For any non-browser consumer, `/v1/events` requires the normal `Authorization`
header, like every other `/v1` route. A query-string token is deliberately
**not** offered: it lands in access
logs and in `Referer` headers, and there is no consumer here that needs it.

### 6.6 Notifications beyond the browser

The stream only reaches somebody who is looking. `catalogue.notifications` is
the durable record, and a small dispatcher on the same advisory-lock leader
forwards `critical` ones outward — a webhook to start with, since that reaches
Slack, email and anything else without this service knowing about any of them.
Deduplicated on `dedup_key`, so a source failing three retries at 03:00 is one
message.

The notifications worth defining first, because they are the failures this plan
exists to catch:

| Kind | Severity | Raised when |
|---|---|---|
| `source.stale` | warning | a source has returned no records for N consecutive runs |
| `source.shrank` | warning | record count fell more than X% against the previous run |
| `job.failed` | warning | a job exhausted `max_attempts` |
| `run.degraded` | warning | a run completed with any failed source |
| `worker.lost` | critical | no heartbeat for three intervals |
| `host.blocking` | critical | 403/429 rate on one host crosses a threshold |
| `schedule.missed` | critical | a schedule's `next_fire_at` passed with no run created |

`source.shrank` and `host.blocking` are the two that justify the whole
notification path. A scraper that quietly starts returning half a catalogue, and
a shop that has started refusing us, are both invisible in a run that reports
success.

---

## 7. The `/ops` section of catalogue-explorer

Server-side queries in a new `src/lib/server/ops.ts`, same pattern as
`explore.ts`. The browser never sees the control token or the database: the SSE
stream is proxied through a SvelteKit `+server.ts` endpoint that adds the token
server-side.

| Route | Content |
|---|---|
| `/ops` | Worker cards: status, heartbeat age, current source, uptime, with pause/resume/drain/stop controls. Last run summary, next scheduled run, queue depth. A **Run now** control with an all-or-pick-sources selector and the run parameters. |
| `/ops/runs` | Run history: kind, who asked, started, duration, sources ok/failed/skipped, records, products retired. |
| `/ops/runs/[id]` | The live view. One row per source: phase, records, requests, rate, errors, a progress bar against the previous run's record count. Per-row pause/resume, cancel and retry. Updates over SSE, falling back to a 5s poll if the stream drops. |
| `/ops/runs/[id]/jobs/[jobId]` | One source in detail: the full log stream with level filter and text search, in-flight requests, the errors list, `field_coverage` from the summary, and a link to the NDJSON artifact. |
| `/ops/sources` | All 80 sources: scraper, last success, records, delta against the previous run, staleness badge, error rate over the last 7 runs. Enable/disable, per-source schedule and parameter overrides. |
| `/ops/notifications` | The durable feed: unacknowledged first, severity filter, acknowledge and link through to the run, job or source that raised it. |
| `/ops/metrics` | Charts (layerchart is already a dependency): records per source over time, run duration, error rate by host, price observations written per day, share of sources stale. |

The staleness badge on `/ops/sources` is the single most useful thing on the
page: a source that silently stopped returning records is the failure mode this
whole plan exists to catch, and today nothing surfaces it.

### 7.1 One stream per tab, in a shared store

The layout subscribes once, in `+layout.svelte`, to
`/ops/events?topics=workers,notifications` and puts the result in a Svelte 5
store. Every page reads from that store, and only `/ops/runs/[id]` opens a
second, narrower subscription (`topics=jobs,progress&run_id=…`) that it closes
on navigate. So the header's worker indicator and the notification badge keep
working on every page without each page owning a connection.

Three details worth building deliberately:

- **Heartbeat age ticks client-side.** The roster arrives with
  `last_heartbeat_at`; a local 1 s interval renders the age. A worker that dies
  therefore goes amber and then red on its own, with no event and no poll — the
  case where waiting for a message is exactly wrong.
- **`resync` is handled, not ignored.** On that event the store re-runs the
  page's normal `load`. Without it, a client that fell behind renders stale data
  for ever and looks like it is working.
- **The connection state is visible.** A small live/reconnecting/offline
  indicator, because an operations page that has quietly stopped updating is
  worse than one that admits it. Falls back to a 5 s poll while disconnected.

---

## 8. Scheduling

No separate scheduler container. Every worker opens a transaction each tick and
tries `pg_try_advisory_xact_lock` on a fixed key; whichever holds it materialises
due `catalogue.schedules` rows into runs and jobs. A transaction-scoped lock is
intentional: it cannot leak or accumulate lock counts on a pooled session. One
less thing to deploy, and no single point of failure.

Firing is idempotent, not merely mutually exclusive. For each due schedule the
leader, in the **same transaction**:

1. locks the schedule row and captures its current `next_fire_at` as `fire_at`;
2. inserts a run carrying `(schedule_id, scheduled_fire_at=fire_at)` with
   `on conflict do nothing` against `runs_scheduled_occurrence_key`;
3. creates jobs only if that insert returned a new run, excluding disabled or
   paused `source_settings` and merging their parameter overrides; and
4. advances `last_fired_at` and computes the following `next_fire_at` in the
   schedule's named timezone.

If the transaction rolls back, neither the run nor the cursor advances. If a
leader dies after commit, the unique occurrence key makes the next tick a no-op
for that fire time. Missed-fire policy is explicit: materialise at most the most
recent missed occurrence after downtime and raise `schedule.missed` for older
ones rather than launching a catch-up herd.

The default schedule: one run at 03:00 Europe/Paris across all enabled sources.
Jobs are staggered by host, so the fan-out does not become a thundering herd.
`source_settings.enabled=false` excludes a source from future runs;
`paused=true` additionally prevents already queued jobs from being claimed.
Re-enabling or resuming does not silently backfill missed occurrences.

**The cache setting matters more than the schedule.** `dump.py` defaults to
`--cache-mode auto --cache-max-age 168`, seven days. A daily run under that
default replays yesterday's cached pages and reports success while updating no
prices at all. The schedule must therefore carry explicit parameters:

- **Daily price run**: `cache_mode=refresh`, or `auto` with `cache_max_age=20`
  hours. Prices are the point of the run; they have to come off the wire.
- **Parser-rework runs**: `cache_mode=replay`, no network, for iterating on
  extraction offline. This is what the cache is genuinely for.

The cache directory becomes a shared named volume so replay works across
workers, with per-host subdirectories to avoid concurrent writers to one entry.

---

## 9. Observability

**Postgres is the system of record**, because the UI needs the data anyway and a
second store would only be a second thing to disagree.

**Structured logs to stdout** as one JSON object per line, using the event
vocabulary from `workers/lifecycle.ts` extended with job events:
`worker.starting`, `worker.ready`, `worker.tick`, `worker.retrying`,
`worker.stopping`, `job.leased`, `job.started`, `job.progress`, `job.loaded`,
`job.failed`, `job.cancelled`, `host.lease.contended`, `run.complete`. Every
line carries `worker_id`, `run_id`, `job_id`, `source`. `journalctl` and
`docker logs` remain usable, and a log shipper can be added later without
touching the code.

**`/metrics` on both the worker and the control service**, Prometheus text
format, no client library needed for this handful:

```
catalogue_jobs{state="queued|running|failed"}       gauge
catalogue_job_duration_seconds{source}              histogram
catalogue_records_collected{source}                 gauge
catalogue_requests_total{source,outcome}            counter
catalogue_http_errors_total{host,status}            counter
catalogue_worker_heartbeat_age_seconds{worker}      gauge
catalogue_source_staleness_seconds{source}          gauge
catalogue_run_duration_seconds                      histogram
catalogue_offers_written_total                      counter
```

Nothing scrapes them yet. They exist so that when something does, no rework is
needed — and `/ops/metrics` reads the same numbers straight from Postgres today.

**Retention**: `job_events` pruned at 30 days by a maintenance job on the same
advisory-lock leader; `job_progress` lives and dies with its job; `runs` and
`jobs` kept indefinitely, they are small.

---

## 10. The catalogue under a proper OpenAPI spec

Today `catalogue-service/app.py` is a hand-rolled `do_GET` with three paths, and
its only description is its docstring. Anything consuming it — the tenant
application, the explorer, a future partner — is reading Python to find out what
comes back.

### 10.1 Two specs, not one

| Spec | Service | Audience | Stability |
|---|---|---|---|
| `catalogue.openapi.json` | `catalogue-service` | tenant applications, external consumers | versioned, breaking changes gated |
| `catalogue-ops.openapi.json` | `catalogue-control` | operators and the explorer's `/ops` | internal, may move faster |

Different audiences, different auth, different guarantees. Merging them would
put a run-cancel endpoint in the document a tenant reads.

### 10.2 Generated, never hand-written

`ateliera-app` already settled this question: zod registries under
`packages/api-contract/src/`, `npm run contracts:generate` emitting
`generated/openapi.json`, a `--check` mode that fails when the checked-in file
has drifted, and `openapi:compat` gating breaking changes. AGENTS.md states the
rule outright — never hand-edit the generated OpenAPI.

The Python side gets the same shape with the same properties:

- Request and response models as **Pydantic** models in a
  `catalogue-service/contracts.py` registry.
- `make openapi` writes `catalogue.openapi.json`; `make openapi-check` fails on
  drift and runs in CI. The spec is checked in, so a reviewer sees the API
  change in the diff.
- A compatibility check against the previous released spec (`oasdiff`) fails the
  build on a breaking change unless the version was bumped.

**This means `catalogue-service` moves onto the same Starlette base as
`catalogue-control`.** A spec generated from a `BaseHTTPRequestHandler` is a
spec written by hand and hoped to be true; six months later it is not. The
stdlib-only argument in that module's docstring was about not carrying a
framework to serve one view, and it was right at the time — but a published,
verified contract is a different requirement and it is worth the dependency.

The read-only property is preserved and made *stronger*, not weaker: today it is
the absence of a write path, which nothing enforces. After the move it is an
assertion in a test — the generated spec contains no operation other than `get`
— and it fails the build if anyone adds one.

### 10.3 Shape corrections to make while specifying

Writing the spec surfaces things the current API does implicitly:

- **`/v1/canonical-products` is two operations wearing one path.** `?ids=` is a
  batch fetch returning offers; `?q=` is a search returning aggregates. They
  return different shapes from one operation id, which OpenAPI can only express
  as a union that every client then has to discriminate. Split them:
  `GET /v1/canonical-products` (search), `GET /v1/canonical-products/{id}`
  (one, with offers), `GET /v1/canonical-products:batch?ids=` (many, with
  offers). Keep the current path answering both for one deprecation window.
- **Errors.** `{"error": "..."}` becomes RFC 9457 `application/problem+json`,
  with `type`, `title`, `status`, `detail`. One error schema referenced from
  every operation instead of an undocumented string.
- **Pagination.** Search takes `limit` capped at 200 and returns no cursor, so
  there is no way to read past the cap. Add cursor pagination (`cursor`,
  `next_cursor`) — the ordering is already deterministic
  (`source_count desc, brand, manufacturer_sku`).
- **Nullability and units.** `min_price_per_litre` is a number with an implied
  currency that is never stated. The spec forces the question: it is a mix of
  currencies today, and it should be EUR at the stated reference rate with the
  rate date in the response, matching what `/compare` already does.
- **`observed_at` freshness.** Every offer should carry when it was collected;
  the fetch path has it, the search path does not, and a consumer cannot tell a
  price from this morning from one from March.

### 10.4 The ops spec and SSE

`catalogue-ops.openapi.json` covers everything in §6. `GET /v1/events` is
described as a `text/event-stream` response — OpenAPI 3.1 can name the media
type but not the schema of each named event, so every payload is defined in
`components/schemas` and the operation description maps SSE event names onto
them:

| `event:` | Schema | Numbered |
|---|---|---|
| `bootstrap` | `Bootstrap` (roster, active run, unacknowledged notifications) | no |
| `worker.roster` | `WorkerRoster` | no |
| `worker.changed` | `WorkerChanged` | yes |
| `run.started` / `run.complete` | `RunEvent` | yes |
| `job.changed` | `JobStateChanged` | yes |
| `job.progress` | `JobProgress` | no |
| `notification.raised` / `notification.resolved` | `Notification` | yes |
| `resync` | `Resync` | no |

The tooling does not enforce the mapping, but the schemas are generated from the
same Pydantic models the service serialises with, so the payloads cannot drift
even though the association between name and schema is prose. Those models also
generate the TypeScript types the explorer's store is written against, which is
what makes §7.1 type-safe rather than a pile of `any`.

### 10.5 What the spec buys immediately

- **Generated TypeScript types for the explorer.**
  `catalogue-explorer/src/lib/catalogue.ts` hand-declares `Product` with 34
  fields and a comment explaining why it lives where it does. Generating it from
  the spec removes a drift source that currently fails silently — a renamed
  column compiles fine and renders blank.
- **A published client for the tenant application**, so `ateliera-app` consumes
  the catalogue through generated types like it does its own API.
- **Contract tests.** Schemathesis runs the spec against the live service in CI
  and finds the responses that do not match what is promised. Given how much of
  this data is nullable, that will find real things.
- **A page to point people at.** The spec renders as documentation; nobody reads
  a docstring in `app.py`.

---

## 11. Deployment

### 11.1 Local, `docker-compose.yml`

Add to the existing stack (Postgres stays on 127.0.0.1:5434):

- `worker` — the collection image. Scaled with `docker compose up --scale
  worker=3`. Shared named volumes: `cache` and `dumps`.
- `worker-browser` — the same code from the camoufox image, started with
  `--capabilities browser`, one replica.
- `control` — the control API, not published on the host.
- `explorer` — the SvelteKit app, published on loopback for development.

The `loader` profile stays exactly as it is, for backfills.

`catalogue-ops-schema.sql` joins the `docker-entrypoint-initdb.d` mounts as
`30-catalogue-ops-schema.sql`, after the two existing schema files.

### 11.2 Production, Quadlet under `makersbrain-infra`

Following the immutable-image and Quadlet approach already documented there
(commit `934a7e2`):

- Two images built and tagged per release: `catalogue-worker` and
  `catalogue-worker-browser`; plus `catalogue-control` and the existing
  `catalogue-service`.
- `catalogue-worker@.service` as a template unit, so instance count is
  `systemctl enable catalogue-worker@{1,2,3}`.
- `catalogue-control.service` and `catalogue-service.service` as plain units.
- Secrets — the database DSN and the control token — from Infisical, using the
  existing `scripts/infisical-populate.sh` path.
- No timer unit: the schedule is in-process behind the advisory lock. (If a
  systemd timer is preferred for visibility, it can `curl` the control API
  instead; the in-process leader is then disabled by config. Worth deciding when
  the units are written, not before.)
- Volumes: the cache and the dump artifacts on a persistent path, with the dumps
  included in the existing backup set. The cache deliberately is not.

---

## 12. Phasing

Each phase is independently shippable and leaves the system working.

| Phase | Work | Result |
|---|---|---|
| **0. Safety net** | pytest, ruff, mypy, CI; replay-mode golden NDJSON per scraper (§4.9) | The refactor can be checked instead of hoped about. Half a week. |
| **1. Package** | `src/` layout, entry points, `dump.py` decomposed (§4.3), pydantic config, TaskGroup/timeouts/SIGTERM (§4.5), structlog + tracing + metrics wiring (§4.6–4.8) | Installable, importable, observable. Output byte-identical, proven by phase 0. |
| **2. Durable run state** | `catalogue-ops-schema.sql`; `ProgressSink` split; `PostgresSink`; `catalogue-dump --run-id` writes to it | Every hand-run crawl leaves a queryable record. No behaviour change. |
| **3. In-process loader** | `load_postgres.py` → `storage/postgres.py` library + CLI; psycopg `copy()`; parameters; one transaction per source; temp staging | `psql` dependency gone; the load is safe to run concurrently. |
| **4. Worker** | `ops/worker.py`, claiming and expired-lease recovery, heartbeats, host lease slots, capabilities, pause/resume/cancel, bounded retry, graceful drain/stop, per-run artifacts | Scrape and load are one automatic step. Runs are triggered by inserting a row. |
| **5. Control API** | `catalogue-control` service, authenticated run/job/worker/source controls; `event_log` + `notifications`; `/v1/events` SSE with topics, bootstrap, watermark reconciliation, `Last-Event-ID` replay, coalescing and backpressure; the notification rules in §6.6 | Runs can be started, controlled, watched and alerted on over HTTP. |
| **6. Ops UI** | `/ops`, `/ops/runs`, `/ops/runs/[id]`, job detail with logs, `/ops/sources`, `/ops/notifications`; the shared layout-level stream store and connection indicator | The whole ask, visible in a browser. |
| **7. Scheduling** | `catalogue.schedules`, advisory-lock leader, daily run at 03:00 with `cache_mode=refresh`, schedules UI | Prices update daily without anyone doing anything. |
| **8. OpenAPI** | Pydantic contract registry, `catalogue-service` onto Starlette, generated + drift-checked `catalogue.openapi.json`, the §10.3 shape corrections, `catalogue-ops.openapi.json`, generated TS types for the explorer | The catalogue is a documented, verified contract instead of a docstring. |
| **9. Metrics and rollout** | `/metrics` endpoints, `/ops/metrics`, retention job, compose services, Quadlet units | Debuggable, deployed, and alertable when a scraper is added. |

Phases 2–4 are the substance. 5–6 are the visible half and are mostly
straightforward once the state is in the database. 7 is small. 9 is packaging.

**Phases 0 and 1 buy nothing visible**, which is exactly why they are easy to
skip and expensive to skip. Everything after them assumes an importable,
installable package with a version, a signal handler, structured logs and a
decomposed orchestrator; bolting a worker onto the current `sys.path`-dependent
scripts means doing this refactor later, under a running service, with real data
flowing through it. Phase 0 is roughly half a week and phase 1 a week, and phase
0 is what makes phase 1 verifiable rather than hopeful.

Phase 8 is independent of 1–7 and could run in parallel or first — it touches
`catalogue-service` and the explorer's types, not the worker. Two things argue
for doing it after the control service exists: `catalogue-control` establishes
the Starlette base that `catalogue-service` then moves onto, and specifying both
APIs together is what keeps the two documents consistent. If the tenant
application needs the contract sooner, phase 8 can be pulled forward and the
control spec written later against the same tooling.

---

## 13. Risks and things to decide while building

- **The refactor is a large change to near-untested code.** Phase 1 touches every
  file outside `scrapers/` and produces no visible improvement, which is the
  profile of a change that quietly alters output. The golden-file tests in phase
  0 are not optional preparation; they are the only thing that makes the "no
  behaviour change" claim checkable. If phase 0 slips, phase 1 should slip with
  it rather than proceed on inspection.
- **Tracing without a collector is scaffolding.** Wiring OpenTelemetry now and
  exporting nowhere is a deliberate bet that the instrumentation is cheap and the
  retrofit is not. It is the right bet, but it should be recognised as unproven
  until something actually consumes a span — until then the useful part is the
  `trace_id` on the job row and in the logs, not the SDK.
- **`pydantic` and `structlog` are the first real dependencies** beyond httpx,
  rich and textual, and OpenTelemetry adds several more. Worth confirming the
  browser image size stays acceptable — camoufox already dominates it, but the
  worker image should not inherit the browser's dependency set (§5.5).
- **Host politeness is the real risk.** Getting a source blocked costs more than
  any feature here is worth. The host lease must be in place *before* more than
  one worker runs — phase 3 has no safe partial delivery on this point.
- **A stale cache defeats the daily run.** See §8. Schedule parameters have to be
  explicit; the default is wrong for this use.
- **Retirement semantics.** `plan_load`'s rules — empty is never grounds for
  retirement, partial only ever adds, truncated is adds-only — protect against
  marking a live catalogue withdrawn. Moving the loader in-process must carry
  them across unchanged, and they deserve tests before the move, not after.
- **Progress write volume.** Throttled at 1 Hz per job with coalesced cumulative
  counters. Worth measuring under a full 80-source run before assuming it holds.
- **The edge/level split is the load-bearing part of the SSE design** (§3.1). If
  progress ever gets written to `event_log` "for consistency", the log becomes
  ~860,000 rows per run, replay becomes unusable and reconnection stops working.
  It is the kind of change that looks tidy in review and is not.
- **A dropped `notify` must never mean a lost event.** Carrying only the id and
  reading the row is what guarantees that. Any future change that puts the
  payload in the `notify` reintroduces both the 8000-byte cap and the loss
  window.
- **Long-lived streams through the proxy chain.** SSE dies quietly to buffering
  or an idle timeout somewhere between Caddy, the SvelteKit proxy and uvicorn,
  and it fails by looking like "nothing is happening" rather than by erroring.
  Test a two-hour connection through the real chain before trusting it — the
  client-side connection indicator exists so this failure is at least visible.
- **Notification thresholds need tuning against real runs.** `source.shrank` at
  a fixed percentage will be noisy for small sources and blind for large ones,
  and an alert nobody trusts is worse than none. Start warning-only, watch a
  fortnight of runs, then set the numbers.
- **Browser memory.** camoufox in a long-lived worker will leak across jobs. The
  browser worker should recycle its process after each job, or after N.
- **Shared cache directory.** Two workers writing one cache entry is a
  corruption path. Per-host subdirectories plus atomic writes; verify against
  `scrapers/cache.py`'s current write strategy.
- **`catalogue.import_runs` overlaps `catalogue.runs`.** They are kept separate
  and linked rather than merged, because `catalogue.load_record` references the
  former. Revisit only if it becomes confusing in the UI.
- **The spec's shape corrections are breaking changes.** Splitting
  `/v1/canonical-products` and moving to `problem+json` change what current
  consumers see. Find out who those are before phase 7 — if it is only the
  explorer and `ateliera-app`, do it in one go; if anything external reads it,
  the deprecation window in §10.3 is not optional.
- **Moving `catalogue-service` onto Starlette contradicts its docstring.** That
  docstring is a considered decision, not an accident, and the plan overrules it
  for a specific reason (§10.2). Update the docstring to say so rather than
  leaving a comment that argues with the code around it.
- **Backfill of history.** The existing `--history-db` SQLite path and the
  `catalogue-dumps*/` directories predate all of this. Decide whether to import
  them into the new run tables or leave them as an archive. Leaving them is
  defensible; the price history that matters is already in
  `catalogue.offer_observations`.
