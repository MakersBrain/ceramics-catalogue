-- Ceramics catalogue: migration for ceramics.catalogue_item.v2 records.
--
-- Apply after catalogue-reference-schema.sql. The baseline loader accepts only
-- the v1 record formats and reads flat `sku`, `quantity` and `unit` fields; v2
-- splits the identity into a manufacturer code and a retailer reference, emits
-- one row per purchasable variant, and carries the package as an object.
--
-- The migration is additive and idempotent: v1 records keep loading unchanged.

begin;

-- 1. Accept the v2 record formats -------------------------------------------

alter table catalogue.source_products
  drop constraint if exists source_products_record_format_check;

alter table catalogue.source_products
  add constraint source_products_record_format_check
  check (record_format in (
    'ceramics.catalogue_item.v1',
    'ceramics.catalogue_identity.v1',
    'ceramics.catalogue_item.v2',
    'ceramics.catalogue_identity.v2'
  ));

-- 2. Promote the fields v2 added --------------------------------------------

alter table catalogue.source_products
  -- Groups the variants of one supplier product.
  add column if not exists parent_external_id text,
  -- The manufacturer's own code (PC-47, SW-229): the cross-supplier key.
  add column if not exists manufacturer_sku text,
  -- The retailer's internal reference, which is not comparable across shops.
  add column if not exists supplier_reference text;

create index if not exists source_products_manufacturer_sku
  on catalogue.source_products (upper(manufacturer_sku))
  where manufacturer_sku is not null;

create index if not exists source_products_parent
  on catalogue.source_products (source_id, parent_external_id)
  where parent_external_id is not null;

alter table catalogue.offer_observations
  -- Price per litre or per kilogram, in the observation's own currency.
  add column if not exists unit_price numeric(18, 6),
  add column if not exists unit_price_per text
    check (unit_price_per is null or unit_price_per in ('l', 'kg'));

-- 3. Teach the loader to read both formats ----------------------------------

create or replace function catalogue.load_record(
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
  v_is_v2 boolean := v_format like '%.v2';
  v_package jsonb := case when jsonb_typeof(p_record->'package_size') = 'object'
                          then p_record->'package_size' end;
  v_firing jsonb := case when jsonb_typeof(p_record->'firing') = 'object'
                         then p_record->'firing' end;
  v_unit_price jsonb := case when jsonb_typeof(p_record->'unit_price') = 'object'
                             then p_record->'unit_price' end;
  v_fetched_at timestamptz;
  v_price numeric;
  v_currency text;
  v_quantity numeric;
  v_unit text;
  v_vat_status text;
  v_sku text;
  v_firing_range text;
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
  if v_format not in (
    'ceramics.catalogue_item.v1', 'ceramics.catalogue_identity.v1',
    'ceramics.catalogue_item.v2', 'ceramics.catalogue_identity.v2'
  ) then
    raise exception 'unsupported_catalogue_record_format: %', v_format using errcode = '22023';
  end if;

  v_fetched_at := (p_record->>'fetched_at')::timestamptz;
  v_price := nullif(p_record->>'price', '')::numeric;
  v_currency := upper(nullif(btrim(p_record->>'currency'), ''));
  v_vat_status := lower(nullif(btrim(p_record->>'vat_status'), ''));
  if v_vat_status is not null and v_vat_status not in ('inclusive', 'exclusive', 'unknown') then
    v_vat_status := 'unknown';
  end if;

  if v_is_v2 then
    -- The package is an object; keep the published value and unit, which the
    -- record already normalised to a single unit per dimension.
    v_quantity := nullif(v_package->>'value', '')::numeric;
    v_unit := lower(nullif(btrim(v_package->>'unit'), ''));
    -- sku stays populated for compatibility with existing queries and views.
    v_sku := coalesce(
      nullif(btrim(p_record->>'manufacturer_sku'), ''),
      nullif(btrim(p_record->>'supplier_reference'), '')
    );
    v_firing_range := nullif(btrim(coalesce(
      v_firing->>'evidence',
      case when v_firing->>'min_celsius' is not null
           then (v_firing->>'min_celsius') || '-' || (v_firing->>'max_celsius') || ' C' end
    )), '');
  else
    v_quantity := nullif(p_record->>'quantity', '')::numeric;
    v_unit := lower(nullif(btrim(p_record->>'unit'), ''));
    v_sku := nullif(btrim(p_record->>'sku'), '');
    v_firing_range := nullif(btrim(p_record->>'firing_range'), '');
  end if;

  insert into catalogue.sources (id)
  values (v_source)
  on conflict (id) do update set updated_at = now();

  insert into catalogue.source_products (
    source_id, external_id, parent_external_id, record_format, name, brand,
    sku, manufacturer_sku, supplier_reference, family, description,
    firing_range, product_url, image_url, availability,
    first_seen_at, last_seen_at, attributes
  ) values (
    v_source, v_external_id,
    nullif(btrim(p_record->>'parent_external_id'), ''),
    v_format, p_record->>'name',
    nullif(btrim(p_record->>'brand'), ''),
    v_sku,
    nullif(btrim(p_record->>'manufacturer_sku'), ''),
    nullif(btrim(p_record->>'supplier_reference'), ''),
    nullif(btrim(p_record->>'family'), ''),
    nullif(btrim(p_record->>'description'), ''),
    v_firing_range,
    p_record->>'product_url',
    nullif(btrim(p_record->>'image_url'), ''),
    nullif(btrim(p_record->>'availability'), ''),
    v_fetched_at, v_fetched_at,
    -- Everything not promoted to a column stays queryable in attributes:
    -- form, surface, effects, colour, claims, documents, technical_attributes,
    -- coats, application_methods, category_path, all_image_urls, unit_price.
    p_record - array[
      'format','source','external_id','parent_external_id','name','brand','sku',
      'manufacturer_sku','supplier_reference','family','description',
      'firing_range','product_url','image_url','availability','price','currency',
      'price_text','vat_status','quantity','unit','fetched_at','raw'
    ]::text[]
  )
  on conflict (source_id, external_id) do update set
    record_format = excluded.record_format,
    parent_external_id = excluded.parent_external_id,
    name = excluded.name,
    brand = excluded.brand,
    sku = excluded.sku,
    manufacturer_sku = excluded.manufacturer_sku,
    supplier_reference = excluded.supplier_reference,
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
      price_text, vat_status, quantity, unit, unit_price, unit_price_per,
      availability, context_sha256, attributes
    ) values (
      v_source_product_id, v_raw_record_id, v_fetched_at, v_price, v_currency,
      nullif(btrim(p_record->>'price_text'), ''), v_vat_status,
      v_quantity, v_unit,
      nullif(v_unit_price->>'value', '')::numeric,
      lower(nullif(btrim(v_unit_price->>'per'), '')),
      nullif(btrim(p_record->>'availability'), ''),
      v_context_hash, coalesce(p_record->'raw', '{}'::jsonb)
    )
    on conflict (source_product_id, observed_at, context_sha256) do nothing;
  end if;

  return v_source_product_id;
end
$$;

-- 4. Compare one manufacturer code across suppliers -------------------------

create or replace view catalogue.offer_comparison as
select
  p.manufacturer_sku,
  p.source_id,
  p.name,
  p.brand,
  p.family,
  p.product_url,
  o.price,
  o.currency,
  o.vat_status,
  o.quantity,
  o.unit,
  o.unit_price,
  o.unit_price_per,
  o.observed_at
from catalogue.source_products p
join lateral (
  select * from catalogue.offer_observations o
  where o.source_product_id = p.id
  order by o.observed_at desc
  limit 1
) o on true
where p.manufacturer_sku is not null and p.active;

comment on view catalogue.offer_comparison is
  'Latest offer per source product, keyed by manufacturer code. Compare only '
  'rows with the same unit_price_per and a similar package quantity: a small '
  'jar always costs more per litre than a large tub.';

commit;
