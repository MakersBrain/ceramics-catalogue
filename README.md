# catalogue-ceramics

Collects public ceramic-materials listings from eighty suppliers, loads them
into PostgreSQL as a comparable reference catalogue, and runs the whole thing on
a schedule that can be watched and controlled from a browser.

```
                    ┌──────────────────────────────────────────┐
                    │  catalogue-explorer  (SvelteKit)         │
                    │  /  /explore  /compare                   │
                    │  /ops  /ops/runs  /ops/sources  …        │
                    └───────┬──────────────────────┬───────────┘
                            │ SSE + JSON           │ SQL (read)
                            │ (proxied server-side)│
                    ┌───────▼──────────┐           │
                    │ catalogue-control│           │
                    │  POST /v1/runs   │           │
                    │  GET  /v1/events │           │
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
```

| Directory | What it is |
|---|---|
| `catalogue-dump/` | the collection package: 12 scrapers, the crawl runner, the loader, the worker |
| `catalogue-control/` | operator API and the live event stream |
| `catalogue-service/` | the read API, under `catalogue.openapi.json` |
| `catalogue-explorer/` | the browser, including `/ops` |
| `deploy/quadlet/` | systemd units for production |

## Getting started

```sh
cp .env.example .env          # then set CATALOGUE_CONTROL_TOKEN
make install
docker compose up -d
docker compose --profile ui up -d     # the explorer on http://127.0.0.1:5175
```

`make` on its own lists every target. `make check` is what a change has to pass.

## The design decisions worth knowing

**Postgres is the queue and the system of record.** It is already running and
the catalogue already lives in it, and the volume is trivial — eighty jobs a
day. `for update skip locked` gives multi-worker claiming; `listen`/`notify`
gives the UI live push. No Redis, no broker, nothing new to back up.

**Edges go in the event log; levels do not.** A job failing is an edge:
discrete, ordered, replayable from `Last-Event-ID`, bad to miss. A job's
counters are a level: only the latest reading means anything. Progress is
therefore written in place and never to `catalogue.event_log` — putting it there
would make the log ~860,000 rows per run and destroy replay.

**LISTEN is a hint, never the queue.** Notifications carry an id and nothing
else, and the control service keeps a watermark it reconciles every five
seconds. A dropped notification costs latency, never data.

**Politeness is per host, per shared edge, and crosses processes.**
`catalogue.hosts` and `host_leases` bound concurrency per shop however many
workers are running. Getting a source blocked costs more than any feature here
is worth. A hostname is not always the whole story: nineteen of these shops are
Shopify storefronts on custom domains and all of them answer from one edge that
meters by client address across every shop on it, so those jobs claim a slot
under `edge:shopify` as well as under their own host. Both keys are ordinary
rows in `catalogue.hosts`, so either bound is an operator's to widen without a
deploy.

**The generated OpenAPI documents are never hand-edited.** Change the Pydantic
registries, run `make openapi`, commit the diff. `make openapi-check` fails the
build on drift, and a test asserts the read API's document contains no operation
other than `get`.

**The golden files are how "no behaviour change" is checked rather than
claimed.** `make test-golden` replays every source the recorded response cache
covers and compares the output against a frozen digest. During the refactor they
caught a typed-config change that silently flipped two scrapers' defaults and
dropped one source from 49 records to 40.

## Testing

| Command | Covers |
|---|---|
| `make test` | the fast suite: no network, no database |
| `make pg-up && make test-postgres` | the queue, run closure, the stream, the loader |
| `make test-golden` | replay every cached source against its frozen dump |
| `make check-all` | all of the above |
