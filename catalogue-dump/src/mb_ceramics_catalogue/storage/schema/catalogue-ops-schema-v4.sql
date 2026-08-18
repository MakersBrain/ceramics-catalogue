-- NATS JetStream delivery state.
--
-- PostgreSQL remains authoritative for job state and fencing, but workers no
-- longer discover work by scanning catalogue.jobs.  A transactional outbox
-- publishes compact job references to JetStream; delivery_generation and
-- execution_token make its at-least-once delivery safe.

begin;

alter table catalogue.jobs
  add column if not exists delivery_generation bigint not null default 1,
  add column if not exists execution_token uuid,
  add column if not exists paused_by_source boolean not null default false;

alter table catalogue.jobs drop constraint if exists jobs_delivery_generation_check;
alter table catalogue.jobs add constraint jobs_delivery_generation_check
  check (delivery_generation > 0);

-- Every host lease belongs to one execution, not merely one logical job.  Two
-- duplicate broker deliveries have the same job id; token-scoped release is
-- what prevents the losing delivery from releasing the winner's politeness
-- slot.
alter table catalogue.host_leases
  add column if not exists execution_token uuid;

create table if not exists catalogue.queue_outbox (
  id               bigint generated always as identity primary key,
  job_id           uuid not null references catalogue.jobs(id) on delete cascade,
  generation       bigint not null check (generation > 0),
  subject          text not null check (subject ~ '^catalogue[.]jobs[.]v1[.]'),
  payload          jsonb not null check (jsonb_typeof(payload) = 'object'),
  available_at     timestamptz not null default now(),
  created_at       timestamptz not null default now(),
  published_at     timestamptz,
  cancelled_at     timestamptz,
  publish_attempts integer not null default 0 check (publish_attempts >= 0),
  last_error       text,
  unique (job_id, generation),
  check (published_at is null or cancelled_at is null)
);

create index if not exists queue_outbox_pending_idx
  on catalogue.queue_outbox (available_at, id)
  where published_at is null and cancelled_at is null;

create index if not exists jobs_execution_expiry_idx
  on catalogue.jobs (lease_expires_at)
  where state in ('leased', 'running');

commit;
