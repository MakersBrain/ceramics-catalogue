-- Sources taken out of runs because their site is failing, and how to tell
-- when it is back.
--
-- `source_settings.enabled = false` is the lever, because it is the one that
-- keeps runs closing: a paused source still has a job created, and that job is
-- acknowledged without ever leaving `queued`, so the run it belongs to never
-- reaches a terminal state. Disabling creates no job at all.
--
-- The cost of that lever is that nothing turns it back on. A supplier whose
-- database is down for a week is not a decision anybody remembers to revisit,
-- and a source silently absent from the catalogue is the failure this table
-- exists to prevent. One row per disabled source, carrying the request that
-- answers "are they back yet".

begin;

create table if not exists catalogue.source_health_probes (
    source_id       text primary key,
    url             text        not null,
    -- 'json' additionally requires a parseable body, which is what separates a
    -- working API from a caching layer answering 200 with an error page.
    expect          text        not null default 'json',
    reason          text,
    disabled_at     timestamptz not null default now(),
    last_checked_at timestamptz,
    last_status     integer,
    last_error      text,
    checks          integer     not null default 0,
    -- Reset to zero by any failure, so a site that flaps between its cache and
    -- its database cannot be counted as recovered by accumulating luck.
    consecutive_ok  integer     not null default 0,
    required_ok     integer     not null default 2,
    recovered_at    timestamptz,
    constraint source_health_probes_expect_check check (expect in ('json', 'ok'))
);

-- The probe loop reads only the rows still waiting on a recovery.
create index if not exists source_health_probes_pending_idx
    on catalogue.source_health_probes (last_checked_at nulls first)
 where recovered_at is null;

commit;
