-- Provider-aware proxy operations. Additive over catalogue-ops-schema.sql.
-- Secrets are deliberately absent: provider credentials live only in the
-- proxy-secrets volume and API keys in the control service secret mount.

begin;

alter table catalogue.proxy_budget_cycles
  add column if not exists id uuid not null default gen_random_uuid(),
  add column if not exists lifecycle text not null default 'active',
  add column if not exists provider_resource_id text,
  add column if not exists unmanaged_allocation_bytes bigint not null default 0,
  add column if not exists proposed_at timestamptz,
  add column if not exists proposed_by text,
  add column if not exists confirmed_at timestamptz,
  add column if not exists confirmed_by text,
  add column if not exists opened_at timestamptz,
  add column if not exists opened_by text,
  add column if not exists closed_at timestamptz,
  add column if not exists closed_by text;

create unique index if not exists proxy_budget_cycles_id_key
  on catalogue.proxy_budget_cycles(id);

alter table catalogue.proxy_budget_cycles
  drop constraint if exists proxy_budget_cycles_lifecycle_check,
  add constraint proxy_budget_cycles_lifecycle_check
    check (lifecycle in ('proposed', 'active', 'closed', 'rejected')),
  drop constraint if exists proxy_budget_cycles_unmanaged_allocation_check,
  add constraint proxy_budget_cycles_unmanaged_allocation_check
    check (unmanaged_allocation_bytes >= 0
       and unmanaged_allocation_bytes <= operational_bytes);

create unique index if not exists proxy_budget_cycles_one_active
  on catalogue.proxy_budget_cycles(provider) where lifecycle = 'active';

create table if not exists catalogue.proxy_profiles (
  id                            uuid primary key default gen_random_uuid(),
  provider                      text not null default 'decodo',
  logical_name                  text not null unique
    check (logical_name ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
  provider_resource_id          text,
  display_name                  text not null,
  username_mask                 text,
  username_fingerprint          text,
  provider_traffic_limit_bytes  bigint check (provider_traffic_limit_bytes >= 0),
  auto_disable                  boolean not null default true,
  enabled                       boolean not null default false,
  lifecycle                     text not null default 'pending'
    check (lifecycle in ('pending', 'enabled', 'draining', 'disabled', 'retired',
                         'provider_changed_local_failed')),
  secret_generation             integer not null default 0 check (secret_generation >= 0),
  secret_installed_at           timestamptz,
  provider_observed_at          timestamptz,
  created_at                    timestamptz not null default now(),
  created_by                    text not null,
  updated_at                    timestamptz not null default now(),
  updated_by                    text not null,
  retired_at                    timestamptz
);

alter table catalogue.proxy_profiles
  add column if not exists pending_action text,
  drop constraint if exists proxy_profiles_pending_action_check,
  add constraint proxy_profiles_pending_action_check
    check (pending_action is null or pending_action in ('disable', 'rotate', 'retire'));

create unique index if not exists proxy_profiles_provider_resource_key
  on catalogue.proxy_profiles(provider, provider_resource_id)
  where provider_resource_id is not null;

create table if not exists catalogue.proxy_profile_allocations (
  provider        text not null,
  cycle_start     timestamptz not null,
  profile_id      uuid not null references catalogue.proxy_profiles(id),
  allocated_bytes bigint not null check (allocated_bytes >= 0),
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  updated_by      text not null,
  primary key (provider, cycle_start, profile_id),
  foreign key (provider, cycle_start)
    references catalogue.proxy_budget_cycles(provider, cycle_start)
);

create table if not exists catalogue.proxy_profile_retirements (
  id                       uuid primary key default gen_random_uuid(),
  profile_id               uuid not null references catalogue.proxy_profiles(id),
  old_provider_resource_id text not null,
  replacement_resource_id  text not null,
  target_limit_bytes       bigint not null check (target_limit_bytes > 0),
  temporary_allocation_bytes bigint not null check (temporary_allocation_bytes > 0),
  old_secret_generation    integer not null check (old_secret_generation >= 0),
  state                    text not null default 'draining'
    check (state in ('creating', 'draining', 'finalizing', 'completed', 'failed')),
  created_at               timestamptz not null default now(),
  completed_at             timestamptz,
  error_code               text
);

alter table catalogue.proxy_profile_retirements
  drop constraint if exists proxy_profile_retirements_state_check,
  add constraint proxy_profile_retirements_state_check
    check (state in ('creating', 'draining', 'finalizing', 'completed', 'failed'));

create table if not exists catalogue.proxy_routes (
  id                  uuid primary key default gen_random_uuid(),
  label               text not null,
  profile_id          uuid not null references catalogue.proxy_profiles(id),
  protocol            text not null default 'http'
    check (protocol in ('http', 'https', 'socks5')),
  country             text check (country is null or country ~ '^[A-Z]{2}$'),
  state               text,
  city                text,
  session_mode        text not null default 'random'
    check (session_mode in ('random', 'sticky')),
  session_minutes     integer not null default 30 check (session_minutes between 1 and 1440),
  max_bytes           bigint not null default 25000000
    check (max_bytes between 1 and 25000000),
  pilot               boolean not null default false,
  enabled             boolean not null default false,
  created_at          timestamptz not null default now(),
  created_by          text not null,
  updated_at          timestamptz not null default now(),
  updated_by          text not null,
  retired_at          timestamptz
);

create table if not exists catalogue.source_proxy_policies (
  source_id            text primary key,
  policy               text not null default 'never'
    check (policy in ('never', 'fallback', 'always')),
  route_id             uuid references catalogue.proxy_routes(id),
  max_bytes            bigint not null default 25000000
    check (max_bytes between 1 and 25000000),
  pilot                boolean not null default true,
  evidence_count       integer not null default 0 check (evidence_count >= 0),
  evidence_state       text not null default 'unproven'
    check (evidence_state in ('unproven', 'eligible', 'promoted', 'rejected')),
  revision             bigint not null default 1 check (revision > 0),
  enabled_at           timestamptz,
  disabled_at          timestamptz,
  updated_at           timestamptz not null default now(),
  updated_by           text not null,
  check ((policy = 'never') or route_id is not null),
  check ((policy <> 'always') or (evidence_state = 'promoted' and evidence_count >= 3))
);

create table if not exists catalogue.proxy_pilot_evidence (
  job_id          uuid primary key references catalogue.jobs(id) on delete cascade,
  source_id       text not null,
  route_id        uuid not null references catalogue.proxy_routes(id),
  succeeded       boolean not null,
  estimated_bytes bigint not null default 0 check (estimated_bytes >= 0),
  recorded_at     timestamptz not null default now(),
  details         jsonb not null default '{}'::jsonb
);

alter table catalogue.jobs
  add column if not exists proxy_snapshot jsonb not null default '{}'::jsonb;

create table if not exists catalogue.proxy_probes (
  id                 uuid primary key default gen_random_uuid(),
  route_id           uuid not null references catalogue.proxy_routes(id),
  profile_id         uuid not null references catalogue.proxy_profiles(id),
  state              text not null default 'pending'
    check (state in ('pending', 'running', 'succeeded', 'failed', 'cancelled')),
  requested_at       timestamptz not null default now(),
  completed_at       timestamptz,
  error_category     text,
  estimated_bytes    bigint not null default 0 check (estimated_bytes >= 0),
  provider_requests  integer not null default 0 check (provider_requests >= 0),
  exit_country       text,
  exit_ip            inet,
  exit_ip_expires_at timestamptz,
  latency_ms         integer check (latency_ms is null or latency_ms >= 0),
  protocol           text not null,
  actor              text not null,
  request_id         uuid not null unique
);

alter table catalogue.proxy_reservations
  alter column job_id drop not null,
  add column if not exists probe_id uuid,
  add column if not exists profile_id uuid references catalogue.proxy_profiles(id),
  add column if not exists route_id uuid references catalogue.proxy_routes(id),
  add column if not exists purpose text not null default 'job',
  add column if not exists secret_generation integer not null default 0,
  add column if not exists revocation_requested boolean not null default false;

alter table catalogue.proxy_reservations
  drop constraint if exists proxy_reservations_job_id_key;

create unique index if not exists proxy_reservations_one_active_job
  on catalogue.proxy_reservations(job_id)
  where job_id is not null and state in ('active', 'revocation_requested');

alter table catalogue.proxy_reservations
  drop constraint if exists proxy_reservations_probe_id_fkey,
  add constraint proxy_reservations_probe_id_fkey
    foreign key (probe_id) references catalogue.proxy_probes(id) on delete cascade,
  drop constraint if exists proxy_reservations_probe_id_key,
  add constraint proxy_reservations_probe_id_key unique (probe_id),
  drop constraint if exists proxy_reservations_consumer_check,
  add constraint proxy_reservations_consumer_check
    check (num_nonnulls(job_id, probe_id) = 1),
  drop constraint if exists proxy_reservations_purpose_check,
  add constraint proxy_reservations_purpose_check
    check ((purpose = 'job' and job_id is not null and probe_id is null)
        or (purpose = 'probe' and probe_id is not null and job_id is null));

alter table catalogue.proxy_reservations
  drop constraint if exists proxy_reservations_state_check,
  add constraint proxy_reservations_state_check
    check (state in ('active', 'closed', 'cancelled', 'revocation_requested'));

create table if not exists catalogue.proxy_provider_snapshots (
  id                   bigint generated always as identity primary key,
  provider             text not null,
  cycle_start          timestamptz not null,
  source_endpoint      text not null
    check (source_endpoint in ('traffic', 'subuser_traffic', 'subscription')),
  grouping_dimension   text not null,
  grouping_key         text not null,
  bucket_start         timestamptz not null,
  bucket_end           timestamptz not null,
  transmitted_bytes    bigint not null default 0 check (transmitted_bytes >= 0),
  received_bytes       bigint not null default 0 check (received_bytes >= 0),
  total_bytes          bigint not null default 0 check (total_bytes >= 0),
  request_count        bigint not null default 0 check (request_count >= 0),
  provider_watermark   text,
  first_observed_at    timestamptz not null default now(),
  last_observed_at     timestamptz not null default now(),
  foreign key (provider, cycle_start)
    references catalogue.proxy_budget_cycles(provider, cycle_start),
  unique (provider, cycle_start, source_endpoint, grouping_dimension,
          grouping_key, bucket_start, bucket_end),
  check (bucket_end > bucket_start)
);

create index if not exists proxy_provider_snapshots_series_idx
  on catalogue.proxy_provider_snapshots(provider, cycle_start, bucket_start);

create table if not exists catalogue.proxy_admin_audit (
  id               bigint generated always as identity primary key,
  operation_id     uuid not null,
  actor            text not null,
  actor_role       text not null check (actor_role in ('viewer', 'admin', 'system')),
  request_id       uuid not null,
  idempotency_key  text,
  action           text not null,
  resource_type    text not null,
  resource_id      text,
  at               timestamptz not null default now(),
  state            text not null default 'started'
    check (state in ('started', 'succeeded', 'failed', 'ambiguous',
                     'provider_changed_local_failed')),
  success          boolean,
  error_code       text,
  before_data      jsonb,
  after_data       jsonb,
  response_status  integer,
  response_data    jsonb
);

create index if not exists proxy_admin_audit_recent_idx
  on catalogue.proxy_admin_audit(at desc, id desc);

create table if not exists catalogue.proxy_mutation_requests (
  operation_id     uuid primary key default gen_random_uuid(),
  actor            text not null,
  action           text not null,
  idempotency_key  text not null,
  state            text not null default 'started'
    check (state in ('started', 'succeeded', 'failed', 'ambiguous',
                     'provider_changed_local_failed')),
  response_status  integer,
  response_data    jsonb,
  created_at       timestamptz not null default now(),
  completed_at     timestamptz,
  unique (actor, action, idempotency_key)
);

create table if not exists catalogue.proxy_actor_nonces (
  nonce       uuid primary key,
  actor       text not null,
  expires_at  timestamptz not null,
  used_at     timestamptz not null default now()
);

create table if not exists catalogue.proxy_reconcile_requests (
  id                  uuid primary key default gen_random_uuid(),
  provider            text not null default 'decodo',
  reason              text not null,
  reservation_id      uuid references catalogue.proxy_reservations(id),
  mutation_request_id uuid,
  dedup_key           text not null unique,
  created_at          timestamptz not null default now(),
  claimed_at          timestamptz,
  completed_at        timestamptz,
  attempts            integer not null default 0 check (attempts >= 0),
  error_code          text
);

create index if not exists proxy_reconcile_requests_pending_idx
  on catalogue.proxy_reconcile_requests(created_at)
  where completed_at is null;

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'catalogue_proxy_maintenance') then
    begin
      create role catalogue_proxy_maintenance nologin;
    exception when insufficient_privilege then
      raise notice 'catalogue_proxy_maintenance role must be created by deployment';
    end;
  end if;
end $$;

create or replace function catalogue.proxy_audit_immutable() returns trigger
language plpgsql security definer set search_path = pg_catalog, catalogue as $$
begin
  if (pg_has_role(current_user, 'catalogue_proxy_maintenance', 'member')
      and current_setting('catalogue.proxy_audit_maintenance', true) = 'on') is not true then
    raise exception 'proxy audit rows are immutable';
  end if;
  return old;
end;
$$;

drop trigger if exists proxy_admin_audit_immutable on catalogue.proxy_admin_audit;
create trigger proxy_admin_audit_immutable
before update or delete on catalogue.proxy_admin_audit
for each row execute function catalogue.proxy_audit_immutable();

alter table catalogue.event_log drop constraint if exists event_log_topic_check;
alter table catalogue.event_log add constraint event_log_topic_check
  check (topic in ('run', 'job', 'worker', 'notification', 'schedule', 'source', 'proxy'));

commit;
