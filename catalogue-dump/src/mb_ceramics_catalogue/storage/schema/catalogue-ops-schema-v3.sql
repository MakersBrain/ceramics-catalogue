-- Reusable scraper output and browser-routing operations.
-- Additive over catalogue-ops-schema.sql and catalogue-ops-schema-v2.sql.

-- `requires` remains the all-of capability set. `requires_any` adds one
-- optional any-of group, used initially to let an auto-browser job run on one
-- of several tested backends. The chosen backend is snapshotted at START, not
-- at claim time, so a lease that is never started leaves no false lineage.
alter table catalogue.jobs
  add column if not exists requires_any text[] not null default '{}',
  add column if not exists selected_browser_backend text;

alter table catalogue.jobs drop constraint if exists jobs_state_check;
alter table catalogue.jobs add constraint jobs_state_check
  check (state in ('queued', 'leased', 'running', 'paused',
                   'succeeded', 'degraded', 'failed', 'cancelled', 'skipped'));

alter table catalogue.jobs drop constraint if exists jobs_selected_browser_backend_check;
alter table catalogue.jobs add constraint jobs_selected_browser_backend_check
  check (selected_browser_backend is null or selected_browser_backend in
         ('camoufox', 'cdp_extension_proxy'));

-- One row per requested dataset contract/projector. Output state is separate
-- from collection completeness: an intact enumeration can still have one
-- failed projector, and a deliberately adds-only dataset can accept an
-- incomplete enumeration without pretending it is complete.
create table if not exists catalogue.job_datasets (
  job_id             uuid not null references catalogue.jobs(id) on delete cascade,
  dataset            text not null check (dataset <> ''),
  contract_version   text not null check (contract_version <> ''),
  projector_version  text not null check (projector_version <> ''),
  state               text not null default 'pending'
    check (state in ('pending', 'projecting', 'staged', 'publishing', 'published',
                     'loading', 'succeeded', 'degraded', 'failed', 'cancelled', 'skipped')),
  complete            boolean not null default false,
  records             bigint not null default 0 check (records >= 0),
  rejected            bigint not null default 0 check (rejected >= 0),
  error               text,
  promoted_at         timestamptz,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  primary key (job_id, dataset, contract_version, projector_version)
);

create index if not exists job_datasets_state_idx
  on catalogue.job_datasets (state, updated_at);

-- A checkpoint is reusable only inside the exact collection identity that
-- produced it. Keeping this metadata first-class prevents a connector/config,
-- dataset selection, or budget change from silently resuming incompatible
-- pages. `budget_state` is a non-secret snapshot of reservations/limits.
create table if not exists catalogue.job_checkpoint_lineages (
  job_id                       uuid not null references catalogue.jobs(id) on delete cascade,
  checkpoint_lineage           uuid not null,
  source_id                    text not null check (source_id <> ''),
  source_url                   text not null check (source_url <> ''),
  connector                    text not null check (connector <> ''),
  connector_version            text not null check (connector_version <> ''),
  connector_configuration      jsonb not null default '{}'::jsonb,
  connector_config_fingerprint text not null
    check (connector_config_fingerprint ~ '^[0-9a-f]{64}$'),
  dataset_fingerprint          text not null check (dataset_fingerprint ~ '^[0-9a-f]{64}$'),
  dataset_selection            jsonb not null default '[]'::jsonb,
  budget_state                 jsonb not null default '{}'::jsonb,
  status                       text not null default 'active'
    check (status in ('active', 'completed', 'rejected', 'expired')),
  created_at                   timestamptz not null default now(),
  updated_at                   timestamptz not null default now(),
  expires_at                   timestamptz,
  checksum                     text check (checksum is null or checksum ~ '^[0-9a-f]{64}$'),
  primary key (job_id, checkpoint_lineage),
  check (expires_at is null or expires_at > created_at),
  check (status <> 'completed' or checksum is not null)
);

create index if not exists job_checkpoint_lineages_status_idx
  on catalogue.job_checkpoint_lineages (status, expires_at);

-- A page commit is the durable boundary for resume. `checkpoint_lineage`
-- prevents a retry that intentionally rejects an old checkpoint from colliding
-- with its page ids. `resume_after` is JSON because connectors own cursor shape.
create table if not exists catalogue.job_pages (
  job_id                uuid not null,
  checkpoint_lineage    uuid not null,
  partition_key          text not null check (partition_key <> ''),
  page_sequence          bigint not null check (page_sequence >= 0),
  page_id                text not null check (page_id <> ''),
  resume_after           jsonb,
  terminal               boolean not null default false,
  enumeration_intact     boolean not null default true,
  connector_version      text not null check (connector_version <> ''),
  committed_at           timestamptz not null default now(),
  primary key (job_id, checkpoint_lineage, partition_key, page_id),
  unique (job_id, checkpoint_lineage, partition_key, page_sequence),
  unique (job_id, checkpoint_lineage, partition_key, page_id, page_sequence),
  foreign key (job_id, checkpoint_lineage)
    references catalogue.job_checkpoint_lineages(job_id, checkpoint_lineage)
    on delete cascade
);

-- `terminal` seals this partition.  An intermediate partition carries the
-- connector-owned cursor for the next declared partition.
alter table catalogue.job_pages drop constraint if exists job_pages_check;

create unique index if not exists job_pages_one_terminal_per_lineage
  on catalogue.job_pages (job_id, checkpoint_lineage, partition_key)
  where terminal;

create index if not exists job_pages_lineage_commit_idx
  on catalogue.job_pages
     (job_id, checkpoint_lineage, partition_key, page_sequence, committed_at);

-- Projected batches and their page manifest are committed in one PostgreSQL
-- transaction. Replaying the same page is therefore an idempotent conflict on
-- this primary key, rather than duplicated output.
create table if not exists catalogue.job_page_batches (
  job_id                uuid not null,
  checkpoint_lineage    uuid not null,
  partition_key          text not null,
  page_id                text not null,
  page_sequence          bigint not null check (page_sequence >= 0),
  dataset                text not null check (dataset <> ''),
  contract_version       text not null check (contract_version <> ''),
  projector_version      text not null check (projector_version <> ''),
  object_key             text not null check (object_key <> ''),
  sha256                 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  size                   bigint not null check (size >= 0),
  records                bigint not null check (records >= 0),
  created_at             timestamptz not null default now(),
  primary key (job_id, checkpoint_lineage, partition_key, page_id, page_sequence, dataset,
               contract_version, projector_version),
  foreign key (job_id, checkpoint_lineage, partition_key, page_id, page_sequence)
    references catalogue.job_pages
      (job_id, checkpoint_lineage, partition_key, page_id, page_sequence)
    on delete cascade,
  foreign key (job_id, dataset, contract_version, projector_version)
    references catalogue.job_datasets(job_id, dataset, contract_version, projector_version)
    on delete cascade,
  unique (object_key)
);

-- Every requested dataset gets an outcome for every committed page, including
-- failures and the later skipped pages caused by a sticky projector failure.
-- Without this table an idempotent replay could verify successful batches but
-- silently change which projector failed.
create table if not exists catalogue.job_page_dataset_outcomes (
  job_id                uuid not null,
  checkpoint_lineage    uuid not null,
  partition_key          text not null,
  page_id                text not null,
  page_sequence          bigint not null check (page_sequence >= 0),
  dataset                text not null,
  contract_version       text not null,
  projector_version      text not null,
  state                  text not null check (state in ('succeeded', 'failed', 'skipped')),
  records                bigint not null default 0 check (records >= 0),
  error                  text,
  primary key (job_id, checkpoint_lineage, partition_key, page_id, page_sequence,
               dataset, contract_version, projector_version),
  foreign key (job_id, checkpoint_lineage, partition_key, page_id, page_sequence)
    references catalogue.job_pages
      (job_id, checkpoint_lineage, partition_key, page_id, page_sequence)
    on delete cascade,
  foreign key (job_id, dataset, contract_version, projector_version)
    references catalogue.job_datasets(job_id, dataset, contract_version, projector_version)
    on delete cascade,
  check ((state = 'failed' and error is not null) or state <> 'failed')
);

-- Immutable published outputs. `location` is an opaque ArtifactStore location;
-- compatibility readers may continue using jobs.artifact_path until migrated.
create table if not exists catalogue.job_artifacts (
  id                   uuid primary key default gen_random_uuid(),
  job_id               uuid not null references catalogue.jobs(id) on delete cascade,
  dataset              text not null check (dataset <> ''),
  contract_version     text not null check (contract_version <> ''),
  projector_version    text not null check (projector_version <> ''),
  kind                 text not null check (kind <> ''),
  location             text not null check (location <> ''),
  sha256               text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  size                 bigint not null check (size >= 0),
  published_at         timestamptz not null default now(),
  available            boolean not null default true,
  retained_at          timestamptz,
  foreign key (job_id, dataset, contract_version, projector_version)
    references catalogue.job_datasets(job_id, dataset, contract_version, projector_version)
    on delete cascade,
  unique (job_id, dataset, contract_version, projector_version, kind),
  unique (location)
);

create index if not exists job_artifacts_job_dataset_idx
  on catalogue.job_artifacts (job_id, dataset, published_at);

alter table catalogue.job_artifacts
  add column if not exists available boolean not null default true;
alter table catalogue.job_artifacts
  add column if not exists retained_at timestamptz;
