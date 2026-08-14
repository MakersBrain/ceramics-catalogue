-- Durable state for running the catalogue: workers, runs, jobs, and the live
-- stream the operations UI reads.
--
-- All in the existing `catalogue` schema. Reference data is untouched; nothing
-- here changes how a record is loaded or promoted.
--
-- The distinction the whole design rests on (§3.1 of PLAN.md) is between
-- **edges** and **levels**:
--
--   * An edge is discrete, meaningful once, and bad to miss — a job failed, a
--     worker stopped, a run completed. Edges go in `catalogue.event_log`, which
--     is where their ordering and their replayability come from.
--   * A level is a current value, meaningful only as the latest reading — a
--     job's counters, a worker's heartbeat. Levels live in `job_progress` and
--     `workers`, updated in place, and are never written to the event log.
--
-- Without that split the log is unusable: a three-hour run of eighty sources
-- emitting progress at 1 Hz is roughly 860,000 rows, all stale within a second
-- of being written, and a client that missed forty progress readings does not
-- want them — it wants the current one.

begin;

-- ---------------------------------------------------------------------------
-- Workers
-- ---------------------------------------------------------------------------

-- Rows are durable so a dead worker is visible as a stale heartbeat rather than
-- as an absence. Nothing fires when a process stops existing, so "when did it
-- last speak" is the only question that can actually be answered about one.
create table if not exists catalogue.workers (
  id                uuid primary key,
  hostname          text not null,
  pid               integer not null,
  version           text,
  capabilities      text[] not null default '{}',   -- e.g. {'browser'}
  started_at        timestamptz not null default now(),
  last_heartbeat_at timestamptz not null default now(),
  status            text not null
    check (status in ('starting', 'idle', 'busy', 'paused', 'draining', 'stopped')),
  desired_state     text not null default 'running'
    check (desired_state in ('running', 'paused', 'draining', 'stopping')),
  current_job_id    uuid
);

create index if not exists workers_live_idx
  on catalogue.workers (last_heartbeat_at desc)
  where status <> 'stopped';

-- ---------------------------------------------------------------------------
-- Schedules, runs and jobs
-- ---------------------------------------------------------------------------

-- What should run, and when. Created before `runs` so the foreign key below can
-- be declared inline rather than added afterwards.
create table if not exists catalogue.schedules (
  id            text primary key,
  enabled       boolean not null default true,
  cron          text not null,                      -- '0 3 * * *'
  timezone      text not null default 'Europe/Paris',
  source_filter jsonb not null default '{"all": true}',
  params        jsonb not null default '{}',
  last_fired_at timestamptz,
  next_fire_at  timestamptz
);

-- One collection run: every job it fanned out to shares this id.
create table if not exists catalogue.runs (
  id                uuid primary key default gen_random_uuid(),
  kind              text not null
    check (kind in ('scheduled', 'manual', 'retry', 'backfill')),
  schedule_id       text references catalogue.schedules(id),
  --  Identity of one cron occurrence, so a leader that dies after committing
  --  cannot cause the same fire time to be materialised twice.
  scheduled_fire_at timestamptz,
  requested_by      text,
  params            jsonb not null default '{}',    -- limit, cache mode, browser, delay
  status            text not null
    check (status in ('queued', 'running', 'complete', 'degraded', 'failed', 'cancelled')),
  created_at        timestamptz not null default now(),
  started_at        timestamptz,
  finished_at       timestamptz,
  summary           jsonb                           -- totals and per-source outcome
);

create unique index if not exists runs_scheduled_occurrence_key
  on catalogue.runs (schedule_id, scheduled_fire_at)
  where schedule_id is not null;

create index if not exists runs_recent_idx on catalogue.runs (created_at desc);
create index if not exists runs_active_idx on catalogue.runs (status)
  where status in ('queued', 'running');

-- Operator-owned overrides. The checked-in sources.json remains the definition
-- of scraper structure; this table is the mutable operational layer over it, so
-- disabling a misbehaving source does not need a deploy.
create table if not exists catalogue.source_settings (
  source_id   text primary key,
  enabled     boolean not null default true,
  paused      boolean not null default false,
  schedule_id text references catalogue.schedules(id),
  params      jsonb not null default '{}',
  updated_at  timestamptz not null default now(),
  updated_by  text
);

-- One source's share of a run. This is the queue.
create table if not exists catalogue.jobs (
  id                uuid primary key default gen_random_uuid(),
  run_id            uuid not null references catalogue.runs(id) on delete cascade,
  source_id         text not null,
  host              text not null,          -- politeness is per host, not per source
  state             text not null
    check (state in ('queued', 'leased', 'running', 'paused',
                     'succeeded', 'failed', 'cancelled', 'skipped')),
  priority          smallint not null default 100,
  attempt           smallint not null default 0,
  max_attempts      smallint not null default 3,
  requires          text[] not null default '{}',   -- capabilities the worker must have
  scheduled_for     timestamptz not null default now(),
  lease_owner       uuid references catalogue.workers(id),
  lease_expires_at  timestamptz,
  cancel_requested  boolean not null default false,
  pause_requested   boolean not null default false,
  --  Set when an operator resumes a durably paused job. A human pause must not
  --  spend the failure budget, so this lets the job be claimed once more
  --  without incrementing `attempt`.
  resume_without_attempt boolean not null default false,
  started_at        timestamptz,
  finished_at       timestamptz,
  error             text,
  trace_id          text,                   -- 32 lower-case hex characters
  artifact_path     text,                   -- run/job-namespaced NDJSON artifact
  artifact_sha256   text,
  artifact_size     bigint,
  summary           jsonb,                  -- the existing run_source() summary, verbatim
  unique (run_id, source_id)
);

-- The claim query's index: it orders by (priority, scheduled_for) over the
-- non-terminal states only, which is a small fraction of a table that is kept
-- indefinitely.
create index if not exists jobs_claimable_idx
  on catalogue.jobs (state, scheduled_for, priority)
  where state in ('queued', 'leased', 'running');

-- The reaper's index: expired leases, for recovering a worker that died.
create index if not exists jobs_expiry_idx
  on catalogue.jobs (lease_expires_at)
  where state in ('leased', 'running');

create index if not exists jobs_run_idx on catalogue.jobs (run_id);
create index if not exists jobs_source_history_idx
  on catalogue.jobs (source_id, finished_at desc)
  where finished_at is not null;

-- Residential proxy budget accounting. All limits are decimal bytes because
-- Decodo sells decimal GB; binary display units must not inflate the allowance.
create table if not exists catalogue.proxy_budget_cycles (
  provider               text not null,
  cycle_start             timestamptz not null,
  cycle_end               timestamptz not null,
  purchased_bytes         bigint not null default 3000000000 check (purchased_bytes > 0),
  operational_bytes       bigint not null default 2400000000 check (operational_bytes > 0),
  daily_bytes             bigint not null default 80000000 check (daily_bytes > 0),
  pilot_bytes             bigint not null default 300000000 check (pilot_bytes > 0),
  pilot_active            boolean not null default false,
  provider_reported_bytes bigint not null default 0 check (provider_reported_bytes >= 0),
  application_bytes       bigint not null default 0 check (application_bytes >= 0),
  reconciled_at           timestamptz,
  reconciliation_ok       boolean not null default false,
  kill_switch             boolean not null default true,
  primary key (provider, cycle_start),
  check (cycle_end > cycle_start),
  check (operational_bytes <= purchased_bytes)
);

create table if not exists catalogue.proxy_reservations (
  id              uuid primary key default gen_random_uuid(),
  job_id          uuid not null unique references catalogue.jobs(id) on delete cascade,
  provider        text not null,
  profile         text not null,
  cycle_start     timestamptz not null,
  reserved_bytes  bigint not null check (reserved_bytes > 0),
  estimated_bytes bigint not null default 0 check (estimated_bytes >= 0),
  request_count   integer not null default 0 check (request_count >= 0),
  pilot           boolean not null default false,
  state           text not null default 'active' check (state in ('active', 'closed', 'cancelled')),
  created_at      timestamptz not null default now(),
  closed_at       timestamptz,
  foreign key (provider, cycle_start)
    references catalogue.proxy_budget_cycles(provider, cycle_start)
);

create index if not exists proxy_reservations_accounting_idx
  on catalogue.proxy_reservations(provider, cycle_start, created_at, state);

-- ---------------------------------------------------------------------------
-- Levels: current values, updated in place, never in the event log
-- ---------------------------------------------------------------------------

-- One row per job. The UI reads this and nothing else for the progress bars.
create table if not exists catalogue.job_progress (
  job_id         uuid primary key references catalogue.jobs(id) on delete cascade,
  updated_at     timestamptz not null default now(),
  phase          text,                      -- discovering|fetching|parsing|paused|loading
  discovered     integer not null default 0,
  records        integer not null default 0,
  requests       integer not null default 0,
  rendered_pages integer not null default 0,
  error_count    integer not null default 0,
  truncated      boolean not null default false,
  in_flight      jsonb not null default '[]'  -- from scrapers.activity, capped at 10
);

alter table catalogue.job_progress
  add column if not exists http_tx_bytes_estimated bigint not null default 0,
  add column if not exists http_rx_bytes_estimated bigint not null default 0,
  add column if not exists browser_tx_bytes_estimated bigint not null default 0,
  add column if not exists browser_rx_bytes_estimated bigint not null default 0,
  add column if not exists cache_bytes_read bigint not null default 0,
  add column if not exists direct_requests integer not null default 0,
  add column if not exists impersonated_requests integer not null default 0,
  add column if not exists browser_requests integer not null default 0,
  add column if not exists proxy_requests integer not null default 0,
  add column if not exists proxy_bytes_reserved bigint not null default 0,
  add column if not exists proxy_bytes_estimated bigint not null default 0,
  add column if not exists browser_gain integer not null default 0,
  add column if not exists browser_zero_gain integer not null default 0,
  add column if not exists outcome_counts jsonb not null default '{}'::jsonb;

-- ---------------------------------------------------------------------------
-- Logs and edges
-- ---------------------------------------------------------------------------

-- Append-only log stream, per job. Pruned on a retention window.
create table if not exists catalogue.job_events (
  id      bigint generated always as identity primary key,
  job_id  uuid not null references catalogue.jobs(id) on delete cascade,
  at      timestamptz not null default now(),
  level   text not null check (level in ('debug', 'info', 'warning', 'error')),
  event   text,                             -- structured name, e.g. 'job.leased'
  message text not null,
  data    jsonb
);

create index if not exists job_events_tail_idx on catalogue.job_events (job_id, id);

-- The ordering authority for everything on the live stream. One bigint sequence
-- across every kind of event, so a browser reconnecting with Last-Event-ID
-- resumes from an exact position rather than approximately.
create table if not exists catalogue.event_log (
  id        bigint generated always as identity primary key,
  at        timestamptz not null default now(),
  topic     text not null
    check (topic in ('run', 'job', 'worker', 'notification', 'schedule', 'source')),
  type      text not null,                  -- job.failed, worker.stopped, run.complete, ...
  run_id    uuid,
  job_id    uuid,
  worker_id uuid,
  source_id text,
  payload   jsonb not null default '{}'
);

create index if not exists event_log_id_topic_idx on catalogue.event_log (id) include (topic);
create index if not exists event_log_run_idx on catalogue.event_log (run_id, id)
  where run_id is not null;

-- ---------------------------------------------------------------------------
-- Politeness across workers
-- ---------------------------------------------------------------------------

-- `HostLimiter` bounds concurrency inside one process. Three worker containers
-- would triple the load on every shop, and getting a source blocked costs more
-- than any feature in this plan is worth.
create table if not exists catalogue.hosts (
  host            text primary key,
  max_concurrency smallint not null default 1 check (max_concurrency > 0),
  delay_seconds   numeric
);

-- One row per usable concurrency slot. Changing max_concurrency reconciles
-- these rows while holding the host row lock; an occupied slot above a reduced
-- limit is never removed until its lease ends.
create table if not exists catalogue.host_leases (
  host         text not null references catalogue.hosts(host) on delete cascade,
  slot         smallint not null check (slot > 0),
  leased_by    uuid references catalogue.workers(id),
  job_id       uuid references catalogue.jobs(id) on delete set null,
  leased_until timestamptz,
  primary key (host, slot)
);

create index if not exists host_leases_free_idx on catalogue.host_leases (host)
  where job_id is null;

-- ---------------------------------------------------------------------------
-- Notifications
-- ---------------------------------------------------------------------------

-- Things an operator should be told about whether or not a browser was open.
-- Durable and acknowledgeable, because the whole point is surviving the night.
create table if not exists catalogue.notifications (
  id              bigint generated always as identity primary key,
  at              timestamptz not null default now(),
  severity        text not null check (severity in ('info', 'warning', 'critical')),
  kind            text not null,            -- job.failed, source.stale, worker.lost, ...
  title           text not null,
  body            text,
  run_id          uuid,
  job_id          uuid,
  source_id       text,
  worker_id       uuid,
  -- Three retries of one source are one notification, not three.
  dedup_key       text not null,
  -- A condition that can end: 'source.stale' clears when the source succeeds.
  resolved_at     timestamptz,
  acknowledged_at timestamptz,
  acknowledged_by text
);

-- Only one *open* notification per dedup key. A resolved or acknowledged one
-- may recur, which is what makes the key a deduplicator rather than a mute.
create unique index if not exists notifications_open_key
  on catalogue.notifications (dedup_key)
  where resolved_at is null and acknowledged_at is null;

create index if not exists notifications_feed_idx on catalogue.notifications (at desc);

-- ---------------------------------------------------------------------------
-- Linking the load back to the crawl that produced it
-- ---------------------------------------------------------------------------

-- `catalogue.import_runs` records the load — a status, a record count, two
-- timestamps — and knew nothing about the crawl that produced it. Kept separate
-- rather than merged because `catalogue.load_record` references it.
alter table catalogue.import_runs
  add column if not exists run_id uuid references catalogue.runs(id);

create index if not exists import_runs_run_idx on catalogue.import_runs (run_id)
  where run_id is not null;

-- ---------------------------------------------------------------------------
-- Notification triggers
-- ---------------------------------------------------------------------------

-- The id only, never the row. Two reasons, both of which have bitten this
-- pattern before:
--
--   1. `notify` payloads are capped at 8000 bytes. A payload carrying
--      `job_progress.in_flight` would exceed it and the *insert* would fail —
--      so a display detail would break the write path.
--   2. `notify` is fire-and-forget. A listener that is reconnecting when one
--      fires loses it permanently. Carrying the id means the notification is a
--      hint to go and read, and the table stays the authority, so a missed hint
--      costs latency and never data.
create or replace function catalogue.notify_event_log() returns trigger
language plpgsql as $$
begin
  perform pg_notify('catalogue_ops', new.id::text);
  return null;
end;
$$;

drop trigger if exists event_log_notify on catalogue.event_log;
create trigger event_log_notify
  after insert on catalogue.event_log
  for each row execute function catalogue.notify_event_log();

-- Progress is a level: disposable, re-snapshotted on stream bootstrap, and so
-- it needs no event-log id and no replay history. It gets its own channel with
-- the job id, and the control service reads that job's current row.
create or replace function catalogue.notify_job_progress() returns trigger
language plpgsql as $$
begin
  perform pg_notify('catalogue_progress', new.job_id::text);
  return null;
end;
$$;

drop trigger if exists job_progress_notify on catalogue.job_progress;
create trigger job_progress_notify
  after insert or update on catalogue.job_progress
  for each row execute function catalogue.notify_job_progress();

-- ---------------------------------------------------------------------------
-- Host reconciliation
-- ---------------------------------------------------------------------------

-- Creates missing slots before a limit rises, and refuses to remove an occupied
-- slot when it falls. Called while holding the host row lock.
create or replace function catalogue.reconcile_host_slots(target_host text)
returns integer
language plpgsql as $$
declare
  limit_now smallint;
  created   integer := 0;
begin
  select max_concurrency into limit_now
    from catalogue.hosts where host = target_host for update;
  if limit_now is null then
    insert into catalogue.hosts (host) values (target_host)
      on conflict (host) do nothing;
    select max_concurrency into limit_now
      from catalogue.hosts where host = target_host for update;
  end if;

  insert into catalogue.host_leases (host, slot)
  select target_host, generate_series(1, limit_now)
  on conflict (host, slot) do nothing;
  get diagnostics created = row_count;

  -- Above the limit and idle: safe to drop. Above the limit and occupied: left
  -- alone, because taking a slot away from a running job does not stop the
  -- requests it is already making.
  delete from catalogue.host_leases
   where host = target_host
     and slot > limit_now
     and job_id is null;

  return created;
end;
$$;

-- The default schedule. Note `cache_mode: refresh`: a daily price run under the
-- old seven-day cache default would replay yesterday's pages and report success
-- while changing no prices at all, which would make the whole schedule a no-op.
insert into catalogue.schedules (id, cron, timezone, params)
values (
  'daily-prices',
  '0 3 * * *',
  'Europe/Paris',
  '{"cache_mode": "refresh", "refresh_mode": "price", "sources": 4, "concurrency": 8}'::jsonb
)
on conflict (id) do nothing;

-- Sources proven to time out or yield zero stay out of the daily freshness
-- promise and run in the weekly diagnostic/full-enrichment window instead.
update catalogue.schedules
   set source_filter = '{"all": true, "except": ["countrylove", "mestrebras", "hobbyland", "toepferspass", "cromartie", "keramik-kriese"]}'::jsonb,
       params = params || '{"refresh_mode": "price"}'::jsonb
 where id = 'daily-prices' and source_filter = '{"all": true}'::jsonb;

insert into catalogue.schedules (id, cron, timezone, source_filter, params)
values (
  'weekly-full', '0 2 * * 0', 'Europe/Paris', '{"all": true}'::jsonb,
  '{"cache_mode": "refresh", "refresh_mode": "full", "sources": 3, "concurrency": 6}'::jsonb
)
on conflict (id) do nothing;

commit;
