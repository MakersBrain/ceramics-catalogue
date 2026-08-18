-- Provider-neutral queue outbox.
-- The selected adapter derives its destination from `route`; no provider
-- subject or queue id is committed with authoritative job state.

begin;

alter table catalogue.queue_outbox
  add column if not exists route text,
  add column if not exists envelope_schema text,
  add column if not exists deduplication_key text;

do $$
begin
  if exists (
    select 1 from information_schema.columns
     where table_schema = 'catalogue' and table_name = 'queue_outbox'
       and column_name = 'subject'
  ) then
    execute $sql$
      update catalogue.queue_outbox
         set route = coalesce(route, payload->>'route',
                              substring(subject from '^catalogue[.]jobs[.]v1[.](.+)$'))
       where route is null
    $sql$;
  else
    update catalogue.queue_outbox set route = payload->>'route' where route is null;
  end if;
end $$;

update catalogue.queue_outbox
   set envelope_schema = coalesce(envelope_schema, payload->>'schema', 'catalogue.job.v1'),
       deduplication_key = coalesce(deduplication_key, job_id::text || ':' || generation::text)
 where envelope_schema is null or deduplication_key is null;

alter table catalogue.queue_outbox
  alter column route set not null,
  alter column envelope_schema set not null,
  alter column envelope_schema set default 'catalogue.job.v1',
  alter column deduplication_key set not null;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'queue_outbox_route_check') then
    alter table catalogue.queue_outbox add constraint queue_outbox_route_check
      check (route in ('plain.normal', 'browser.auto.normal',
                       'browser.camoufox.normal',
                       'browser.cdp_extension_proxy.normal'));
  end if;
  if not exists (select 1 from pg_constraint where conname = 'queue_outbox_schema_check') then
    alter table catalogue.queue_outbox add constraint queue_outbox_schema_check
      check (envelope_schema = 'catalogue.job.v1');
  end if;
  if not exists (
    select 1 from pg_constraint where conname = 'queue_outbox_deduplication_key_unique'
  ) then
    alter table catalogue.queue_outbox add constraint queue_outbox_deduplication_key_unique
      unique (deduplication_key);
  end if;
end $$;

alter table catalogue.queue_outbox drop column if exists subject;

commit;
