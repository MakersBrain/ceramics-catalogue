-- Searchable reference catalogue for ceramics supplier NDJSON exports.
--
-- This schema is deliberately global rather than tenant-owned. It describes
-- public manufacturer/supplier catalogue facts that may be reused by every
-- Ateliera module. Tenant products can link to catalogue.canonical_products in
-- a later application migration without copying supplier observations.
--
-- Requires pgcrypto, already installed by the Ateliera platform baseline.

create schema if not exists catalogue;

create table catalogue.sources (
  id text primary key check (id ~ '^[a-z0-9][a-z0-9-]*$'),
  label text,
  homepage_url text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  metadata jsonb not null default '{}'::jsonb,
  check (label is null or btrim(label) <> ''),
  check (homepage_url is null or btrim(homepage_url) <> ''),
  check (jsonb_typeof(metadata) = 'object')
);

create table catalogue.import_runs (
  id uuid primary key default gen_random_uuid(),
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  status text not null default 'running'
    check (status in ('running', 'complete', 'failed')),
  record_count integer not null default 0 check (record_count >= 0),
  error_count integer not null default 0 check (error_count >= 0),
  importer_version text,
  metadata jsonb not null default '{}'::jsonb,
  check (finished_at is null or finished_at >= started_at),
  check (jsonb_typeof(metadata) = 'object')
);

create table catalogue.source_documents (
  id uuid primary key default gen_random_uuid(),
  source_id text not null references catalogue.sources(id),
  import_run_id uuid references catalogue.import_runs(id) on delete set null,
  url text not null check (btrim(url) <> ''),
  title text,
  media_type text,
  published_on date,
  fetched_at timestamptz not null,
  content_sha256 bytea,
  metadata jsonb not null default '{}'::jsonb,
  unique (source_id, url, fetched_at),
  check (content_sha256 is null or octet_length(content_sha256) = 32),
  check (jsonb_typeof(metadata) = 'object')
);
create index source_documents_source_time
  on catalogue.source_documents (source_id, fetched_at desc);

-- A canonical product is an optional curated identity. Imports never guess
-- that similarly named supplier rows are the same product.
create table catalogue.canonical_products (
  id uuid primary key default gen_random_uuid(),
  brand text,
  manufacturer_sku text,
  name text not null check (btrim(name) <> ''),
  family text,
  description text,
  firing_range text,
  attributes jsonb not null default '{}'::jsonb,
  active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (brand is null or btrim(brand) <> ''),
  check (manufacturer_sku is null or btrim(manufacturer_sku) <> ''),
  check (jsonb_typeof(attributes) = 'object')
);
create unique index canonical_products_brand_sku
  on catalogue.canonical_products (lower(brand), lower(manufacturer_sku))
  where brand is not null and manufacturer_sku is not null;

-- One row per identity in an NDJSON source. external_id is the stable source
-- key; SKU is not globally unique and is intentionally not a key.
create table catalogue.source_products (
  id uuid primary key default gen_random_uuid(),
  source_id text not null references catalogue.sources(id),
  external_id text not null check (btrim(external_id) <> ''),
  canonical_product_id uuid references catalogue.canonical_products(id),
  record_format text not null
    check (record_format in (
      'ceramics.catalogue_item.v1',
      'ceramics.catalogue_identity.v1'
    )),
  name text not null check (btrim(name) <> ''),
  brand text,
  sku text,
  family text,
  description text,
  firing_range text,
  product_url text not null check (btrim(product_url) <> ''),
  image_url text,
  availability text,
  first_seen_at timestamptz not null,
  last_seen_at timestamptz not null,
  active boolean not null default true,
  attributes jsonb not null default '{}'::jsonb,
  unique (source_id, external_id),
  check (brand is null or btrim(brand) <> ''),
  check (sku is null or btrim(sku) <> ''),
  check (last_seen_at >= first_seen_at),
  check (jsonb_typeof(attributes) = 'object')
);
create index source_products_sku
  on catalogue.source_products (lower(sku)) where sku is not null;
create index source_products_brand_family
  on catalogue.source_products (lower(brand), lower(family));
create index source_products_canonical
  on catalogue.source_products (canonical_product_id)
  where canonical_product_id is not null;
create index source_products_search
  on catalogue.source_products using gin (
    to_tsvector(
      'simple',
      coalesce(name, '') || ' ' || coalesce(brand, '') || ' ' ||
      coalesce(sku, '') || ' ' || coalesce(family, '') || ' ' ||
      coalesce(description, '')
    )
  );

-- Exact imported JSON is retained for audit/reprocessing. Re-importing the
-- same JSON for the same source identity is idempotent.
create table catalogue.raw_records (
  id bigint generated by default as identity primary key,
  source_product_id uuid not null
    references catalogue.source_products(id) on delete cascade,
  document_id uuid references catalogue.source_documents(id) on delete set null,
  import_run_id uuid references catalogue.import_runs(id) on delete set null,
  fetched_at timestamptz not null,
  record_sha256 bytea not null check (octet_length(record_sha256) = 32),
  record jsonb not null check (jsonb_typeof(record) = 'object'),
  unique (source_product_id, record_sha256)
);
create index raw_records_product_time
  on catalogue.raw_records (source_product_id, fetched_at desc);

-- Append-only observations. A product may have no observations (manufacturer
-- brochure), one, or several package-specific observations (price list/shop).
create table catalogue.offer_observations (
  id bigint generated by default as identity primary key,
  source_product_id uuid not null
    references catalogue.source_products(id) on delete cascade,
  raw_record_id bigint references catalogue.raw_records(id) on delete set null,
  observed_at timestamptz not null,
  price numeric(18, 6) not null check (price >= 0),
  currency text not null check (currency ~ '^[A-Z]{3}$'),
  price_text text,
  vat_status text check (vat_status is null or vat_status in ('inclusive', 'exclusive', 'unknown')),
  quantity numeric(18, 6) check (quantity is null or quantity > 0),
  unit text,
  availability text,
  context_sha256 bytea not null check (octet_length(context_sha256) = 32),
  attributes jsonb not null default '{}'::jsonb,
  unique (source_product_id, observed_at, context_sha256),
  check ((quantity is null) = (unit is null)),
  check (unit is null or btrim(unit) <> ''),
  check (jsonb_typeof(attributes) = 'object')
);
create index offer_observations_product_time
  on catalogue.offer_observations (source_product_id, observed_at desc);
create index offer_observations_currency_price
  on catalogue.offer_observations (currency, price);

create view catalogue.latest_offers as
select distinct on (source_product_id)
       id, source_product_id, raw_record_id, observed_at, price, currency,
       price_text, vat_status, quantity, unit, availability, attributes
  from catalogue.offer_observations
 order by source_product_id, observed_at desc, id desc;

-- Upsert one decoded NDJSON object. The caller owns transaction boundaries so
-- an entire file can be loaded atomically. Identity-only rows intentionally do
-- not create an offer observation.
create function catalogue.load_record(
  p_record jsonb,
  p_import_run_id uuid default null,
  p_document_id uuid default null
) returns uuid
language plpgsql
set search_path = pg_catalog, catalogue
as $$
declare
  v_source_product_id uuid;
  v_raw_record_id bigint;
  v_source text := p_record->>'source';
  v_external_id text := p_record->>'external_id';
  v_format text := p_record->>'format';
  v_fetched_at timestamptz;
  v_price numeric;
  v_currency text;
  v_quantity numeric;
  v_unit text;
  v_vat_status text;
  v_record_hash bytea;
  v_context_hash bytea;
begin
  if jsonb_typeof(p_record) <> 'object' then
    raise exception 'catalogue_record_must_be_an_object' using errcode = '22023';
  end if;
  if nullif(btrim(v_source), '') is null
     or nullif(btrim(v_external_id), '') is null
     or nullif(btrim(p_record->>'name'), '') is null
     or nullif(btrim(p_record->>'product_url'), '') is null then
    raise exception 'catalogue_record_missing_required_identity' using errcode = '22023';
  end if;
  if v_format not in ('ceramics.catalogue_item.v1', 'ceramics.catalogue_identity.v1') then
    raise exception 'unsupported_catalogue_record_format: %', v_format using errcode = '22023';
  end if;

  v_fetched_at := (p_record->>'fetched_at')::timestamptz;
  v_price := nullif(p_record->>'price', '')::numeric;
  v_currency := upper(nullif(btrim(p_record->>'currency'), ''));
  v_quantity := nullif(p_record->>'quantity', '')::numeric;
  v_unit := lower(nullif(btrim(p_record->>'unit'), ''));
  v_vat_status := lower(nullif(btrim(p_record->>'vat_status'), ''));
  if v_vat_status is not null and v_vat_status not in ('inclusive', 'exclusive', 'unknown') then
    v_vat_status := 'unknown';
  end if;

  insert into catalogue.sources (id)
  values (v_source)
  on conflict (id) do update set updated_at = now();

  insert into catalogue.source_products (
    source_id, external_id, record_format, name, brand, sku, family,
    description, firing_range, product_url, image_url, availability,
    first_seen_at, last_seen_at, attributes
  ) values (
    v_source, v_external_id, v_format, p_record->>'name',
    nullif(btrim(p_record->>'brand'), ''), nullif(btrim(p_record->>'sku'), ''),
    nullif(btrim(p_record->>'family'), ''), nullif(btrim(p_record->>'description'), ''),
    nullif(btrim(p_record->>'firing_range'), ''), p_record->>'product_url',
    nullif(btrim(p_record->>'image_url'), ''), nullif(btrim(p_record->>'availability'), ''),
    v_fetched_at, v_fetched_at,
    p_record - array[
      'format','source','external_id','name','brand','sku','family','description',
      'firing_range','product_url','image_url','availability','price','currency',
      'price_text','vat_status','quantity','unit','fetched_at','raw'
    ]::text[]
  )
  on conflict (source_id, external_id) do update set
    record_format = excluded.record_format,
    name = excluded.name,
    brand = excluded.brand,
    sku = excluded.sku,
    family = excluded.family,
    description = excluded.description,
    firing_range = excluded.firing_range,
    product_url = excluded.product_url,
    image_url = excluded.image_url,
    availability = excluded.availability,
    last_seen_at = greatest(catalogue.source_products.last_seen_at, excluded.last_seen_at),
    active = true,
    attributes = excluded.attributes
  returning id into v_source_product_id;

  v_record_hash := digest(convert_to(p_record::text, 'UTF8'), 'sha256');
  insert into catalogue.raw_records (
    source_product_id, document_id, import_run_id, fetched_at,
    record_sha256, record
  ) values (
    v_source_product_id, p_document_id, p_import_run_id, v_fetched_at,
    v_record_hash, p_record
  )
  on conflict (source_product_id, record_sha256) do update set
    document_id = coalesce(catalogue.raw_records.document_id, excluded.document_id),
    import_run_id = coalesce(catalogue.raw_records.import_run_id, excluded.import_run_id)
  returning id into v_raw_record_id;

  if v_price is not null then
    if v_currency is null then
      raise exception 'priced_catalogue_record_requires_currency' using errcode = '22023';
    end if;
    if (v_quantity is null) <> (v_unit is null) then
      raise exception 'catalogue_quantity_and_unit_must_be_paired' using errcode = '22023';
    end if;
    v_context_hash := digest(convert_to(jsonb_build_object(
      'price', v_price, 'currency', v_currency,
      'vat_status', v_vat_status, 'quantity', v_quantity,
      'unit', v_unit, 'availability', p_record->>'availability'
    )::text, 'UTF8'), 'sha256');
    insert into catalogue.offer_observations (
      source_product_id, raw_record_id, observed_at, price, currency,
      price_text, vat_status, quantity, unit, availability,
      context_sha256, attributes
    ) values (
      v_source_product_id, v_raw_record_id, v_fetched_at, v_price, v_currency,
      nullif(btrim(p_record->>'price_text'), ''), v_vat_status,
      v_quantity, v_unit, nullif(btrim(p_record->>'availability'), ''),
      v_context_hash, coalesce(p_record->'raw', '{}'::jsonb)
    )
    on conflict (source_product_id, observed_at, context_sha256) do nothing;
  end if;

  return v_source_product_id;
end
$$;

-- Search source identities and return their latest observed offer, if any.
create function catalogue.search_products(p_query text, p_limit integer default 50)
returns table (
  source_product_id uuid,
  canonical_product_id uuid,
  source_id text,
  external_id text,
  name text,
  brand text,
  sku text,
  family text,
  firing_range text,
  product_url text,
  image_url text,
  price numeric,
  currency text,
  quantity numeric,
  unit text,
  observed_at timestamptz,
  rank real
)
language sql stable
set search_path = pg_catalog, catalogue
as $$
  with query as (
    select websearch_to_tsquery('simple', p_query) as value
  )
  select p.id, p.canonical_product_id, p.source_id, p.external_id,
         p.name, p.brand, p.sku, p.family, p.firing_range,
         p.product_url, p.image_url,
         offer.price, offer.currency, offer.quantity, offer.unit,
         offer.observed_at,
         ts_rank_cd(
           to_tsvector(
             'simple',
             coalesce(p.name, '') || ' ' || coalesce(p.brand, '') || ' ' ||
             coalesce(p.sku, '') || ' ' || coalesce(p.family, '') || ' ' ||
             coalesce(p.description, '')
           ), query.value
         ) as rank
    from catalogue.source_products p
    cross join query
    left join catalogue.latest_offers offer on offer.source_product_id = p.id
   where p.active
     and (
       btrim(p_query) = ''
       or to_tsvector(
            'simple',
            coalesce(p.name, '') || ' ' || coalesce(p.brand, '') || ' ' ||
            coalesce(p.sku, '') || ' ' || coalesce(p.family, '') || ' ' ||
            coalesce(p.description, '')
          ) @@ query.value
       or lower(p.sku) = lower(p_query)
     )
   order by rank desc, p.name, p.source_id
   limit least(greatest(p_limit, 1), 200)
$$;

comment on schema catalogue is
  'Reusable public ceramics catalogue imported from audited NDJSON sources.';
comment on table catalogue.source_products is
  'Source-scoped identities; no automatic cross-supplier product merging.';
comment on table catalogue.canonical_products is
  'Curated identities used to merge known-equivalent source products.';
comment on table catalogue.offer_observations is
  'Append-only package and price observations; absent for identity-only guides.';
