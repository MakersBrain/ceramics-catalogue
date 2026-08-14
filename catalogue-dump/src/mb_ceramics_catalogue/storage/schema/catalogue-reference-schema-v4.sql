-- Hot explorer expressions promoted out of JSON, plus a coherent catalogue
-- generation used to invalidate process-local aggregate caches.
begin;

alter table catalogue.source_products
  add column if not exists facet_colour text
    generated always as (attributes->'colour'->>'name') stored,
  add column if not exists facet_surface text
    generated always as (attributes->>'surface') stored,
  add column if not exists facet_form text
    generated always as (attributes->>'form') stored,
  add column if not exists facet_firing_max numeric
    generated always as (
      case when attributes->'firing'->>'max_celsius' ~ '^-?[0-9]+(?:\.[0-9]+)?$'
           then (attributes->'firing'->>'max_celsius')::numeric end
    ) stored,
  add column if not exists facet_package_size numeric
    generated always as (
      case
        when attributes->'package_size'->>'millilitres' ~ '^-?[0-9]+(?:\.[0-9]+)?$'
          then (attributes->'package_size'->>'millilitres')::numeric
        when attributes->'package_size'->>'grams' ~ '^-?[0-9]+(?:\.[0-9]+)?$'
          then (attributes->'package_size'->>'grams')::numeric
      end
    ) stored,
  add column if not exists facet_application_methods jsonb
    generated always as (
      case when jsonb_typeof(attributes->'application_methods') = 'array'
           then attributes->'application_methods' else '[]'::jsonb end
    ) stored;

create table if not exists catalogue.catalogue_generation (
  singleton boolean primary key default true check (singleton),
  generation bigint not null default 0,
  promoted_at timestamptz
);
insert into catalogue.catalogue_generation(singleton) values (true)
on conflict (singleton) do nothing;

commit;
