-- Ceramics catalogue: promote curated manufacturer identities.
--
-- Apply after catalogue-reference-schema.sql and catalogue-reference-schema-v2.sql.
--
-- `catalogue.canonical_products` is the only layer with a stable cross-supplier
-- key, and it is deliberately never filled by the importer: a loader that guessed
-- which similarly named supplier rows are the same product would silently merge
-- two different glazes, and nothing downstream could tell that it had. This file
-- supplies the missing curation as an explicit, auditable rule instead.
--
-- The rule is narrow on purpose. A supplier row is promoted only when
--
--   1. its `brand` resolves, through a hand-written alias, to a row in
--      `catalogue.manufacturers` - so a retailer's own label never becomes a
--      manufacturer, and
--   2. it carries a `manufacturer_sku` - the maker's own code, which the dump
--      already refuses to invent from a shop's internal article number.
--
-- Everything else stays exactly where it is. Promotion adds a curated identity
-- and links supplier rows to it; it never edits, merges or retires a supplier row.
--
-- Idempotent and re-runnable:
--
--   psql -d ateliera -f catalogue-dump/catalogue-canonical-promotion.sql
--   select * from catalogue.promote_canonical_products();
--   select * from catalogue.promote_canonical_products('mayco');   -- one maker

begin;

-- 1. Who counts as a manufacturer ------------------------------------------
--
-- This table is the allowlist, and it exists because the data cannot answer the
-- question. `brand` on a supplier row is whatever that shop printed: Ceradel
-- sells AMACO and Mayco under `brand: "Harry-Ceradel"`, and Ulster Ceramics
-- Pottery Supplies appears as a brand on its own listings. Both carry article
-- numbers that look exactly like manufacturer codes. Only a person knows which
-- of these names belongs to a company that actually makes glaze.

create table if not exists catalogue.manufacturers (
  id text primary key check (id ~ '^[a-z0-9][a-z0-9-]*$'),
  name text not null check (btrim(name) <> ''),
  homepage_url text check (homepage_url is null or btrim(homepage_url) <> ''),
  -- The maker's own storefront in catalogue.sources, where it has one. A row
  -- from that source outranks any retailer's copy of the same product.
  source_id text references catalogue.sources(id),
  active boolean not null default true,
  notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- The display name reaches canonical_products.brand, which carries its own
-- unique index on (lower(brand), lower(manufacturer_sku)). Two manufacturers
-- sharing a display name would collide there rather than here, which is a much
-- harder failure to read.
create unique index if not exists manufacturers_name
  on catalogue.manufacturers (lower(name));

-- Every spelling a shop has used for a maker. Case and punctuation vary per
-- storefront ("Mayco"/"MAYCO", "Speedball"/"Speedball Art"), and a missing alias
-- silently splits one maker's catalogue in two.
create table if not exists catalogue.manufacturer_aliases (
  alias text primary key
    check (btrim(alias) <> '' and alias = lower(alias)),
  manufacturer_id text not null
    references catalogue.manufacturers(id) on delete cascade
);

create index if not exists manufacturer_aliases_manufacturer
  on catalogue.manufacturer_aliases (manufacturer_id);

-- 2. What a promoted canonical product carries ------------------------------

alter table catalogue.canonical_products
  add column if not exists manufacturer_id text references catalogue.manufacturers(id),
  -- The maker's code with punctuation removed. No source in the current dump
  -- writes one code two ways, so this changes nothing today; it is here so that
  -- the next shop to publish "PC-20" where another writes "PC20" lands on the
  -- existing identity instead of quietly creating a second one.
  add column if not exists sku_key text,
  -- A hand-curated row is never overwritten by a promotion run.
  add column if not exists origin text not null default 'curated'
    check (origin in ('curated', 'promoted'));

create unique index if not exists canonical_products_manufacturer_sku_key
  on catalogue.canonical_products (manufacturer_id, sku_key)
  where manufacturer_id is not null and sku_key is not null;

create or replace function catalogue.sku_key(p_sku text)
returns text
language sql
immutable
set search_path = pg_catalog
as $$
  select nullif(upper(regexp_replace(coalesce(p_sku, ''), '[^A-Za-z0-9]', '', 'g')), '');
$$;

-- A canonical name is a display string, so it carries neither the pack nor the
-- firing range: both are already columns, and repeating them in the name is what
-- makes one product look like four.
--
-- The labels are a closed list read off the loaded dump rather than a general
-- "strip anything before a colon" rule, which would turn "Stroke & Coat: Hot
-- Tamale" into "Stroke & Coat" and lose the product.
create or replace function catalogue.clean_product_name(p_name text)
returns text
language sql
immutable
set search_path = pg_catalog
as $$
  with stripped as (
    select coalesce(p_name, '') as n
  ), packed as (
    -- "Formato: 473 ml", "Poids: 1kg", "Size /Unit of Measure: SD-217 10lbs Dry"
    select regexp_replace(
      n,
      '\s*[–—|-]?\s*(Size\s*/\s*Unit of Measure|Unit of Measure|Size|Formato|Formaat|Format|Poids|Peso|T[uū]ris|Gr[oö]sse|Gr[oö]ße|Taille|Inhalt|Contenu|Content|Volume)\s*:\s*.*$',
      '', 'i') as n from stripped
  ), unlabelled as (
    -- The same clause written without a label: "– POT DE 500ML", "- SEAU DE 5KG".
    select regexp_replace(
      n,
      -- \y, not \b: in a POSIX regular expression \b is a backspace, and Postgres
      -- spells the word boundary \y.
      '\s*[–—|-]\s*(POT|SEAU|SACHET|SAC|BOITE|BO[iî]TE|FLACON|JAR|BUCKET|BAG)\y.*$',
      '', 'i') as n from packed
  ), fired as (
    -- "– 1200/1280°C", which firing_range already holds. Stripped after the pack
    -- clause, because these titles print the firing range before it.
    select regexp_replace(
      n,
      '\s*[–—-]\s*[0-9]{3,4}\s*[/-]\s*[0-9]{3,4}\s*°?\s*C\s*$',
      '', 'i') as n from unlabelled
  )
  select nullif(btrim(regexp_replace(n, '\s+', ' ', 'g'), ' –—-,/'), '') from fired;
$$;

-- 3. The promotion ----------------------------------------------------------

create or replace function catalogue.promote_canonical_products(
  p_manufacturer text default null
) returns table (
  manufacturers int,
  canonical_created int,
  canonical_updated int,
  source_products_linked int
)
language plpgsql
set search_path = pg_catalog, catalogue
as $$
declare
  v_created int := 0;
  v_updated int := 0;
  v_linked int := 0;
  v_makers int := 0;
begin
  if p_manufacturer is not null
     and not exists (select 1 from catalogue.manufacturers where id = p_manufacturer) then
    raise exception 'unknown_manufacturer: %', p_manufacturer using errcode = '22023';
  end if;

  -- Adopt any hand-curated row that predates this file, so the upsert below has
  -- one arbiter index rather than two. Without this a curated ('Mayco','SC74')
  -- row with no manufacturer_id would not match the (manufacturer_id, sku_key)
  -- conflict target, and the insert would fail against the older brand+sku index
  -- instead of updating.
  update catalogue.canonical_products c
     set manufacturer_id = m.id,
         sku_key = catalogue.sku_key(c.manufacturer_sku),
         updated_at = now()
    from catalogue.manufacturer_aliases a
    join catalogue.manufacturers m on m.id = a.manufacturer_id
   where c.manufacturer_id is null
     and c.brand is not null
     and c.manufacturer_sku is not null
     and a.alias = lower(btrim(c.brand))
     and (p_manufacturer is null or m.id = p_manufacturer);

  with candidate as (
    select
      m.id   as manufacturer_id,
      m.name as manufacturer_name,
      upper(btrim(sp.manufacturer_sku)) as sku,
      catalogue.sku_key(sp.manufacturer_sku) as sku_key,
      sp.id, sp.source_id, sp.name, sp.description, sp.firing_range,
      sp.family, sp.image_url, sp.last_seen_at,
      coalesce(catalogue.clean_product_name(sp.name), sp.name) as clean_name,
      -- The maker's own listing beats every retailer's copy of it. Retailer
      -- titles are written for a search box: "POTTER'S CHOICE | PC-38 IRON
      -- YELLOW | AMACO 472 ml / Amarelo / Alta Temperatura" is one product name
      -- in this dump.
      (m.source_id is not null and sp.source_id = m.source_id) as from_maker,
      (sp.description is not null)::int
        + (sp.firing_range is not null)::int
        + (sp.family is not null)::int
        + (sp.image_url is not null)::int as completeness
    from catalogue.source_products sp
    join catalogue.manufacturer_aliases a on a.alias = lower(btrim(sp.brand))
    join catalogue.manufacturers m on m.id = a.manufacturer_id and m.active
    where sp.active
      and sp.brand is not null
      and catalogue.sku_key(sp.manufacturer_sku) is not null
      and (p_manufacturer is null or m.id = p_manufacturer)
  ),
  ranked as (
    select c.*,
           row_number() over (partition by manufacturer_id, sku_key
                              order by from_maker desc, completeness desc,
                                       last_seen_at desc, id) as rnk,
           -- The name is ranked by length and NOT by provenance, which is the
           -- opposite of every other field here. A maker's own storefront writes
           -- its titles for its own variant selector - Mayco publishes "Hot
           -- Tamale Size /Unit of Measure: Pint" - so preferring the maker picks
           -- the worst title on offer. Retailers name the product because that
           -- is what a customer searches for, and the shortest of their titles
           -- is reliably the bare product name.
           --
           -- The one thing shortest-wins gets wrong is a shop whose title is
           -- just the code, so a title that reduces to the SKU is ranked last.
           row_number() over (partition by manufacturer_id, sku_key
                              order by (catalogue.sku_key(clean_name) = sku_key),
                                       length(clean_name), from_maker desc,
                                       last_seen_at desc, id) as rnk_name
      from candidate c
  ),
  -- Field by field, the best non-null value rather than one row wholesale: the
  -- maker publishes the firing range and no price, a retailer publishes an image
  -- and no range, and a canonical identity wants both.
  merged as (
    select
      manufacturer_id,
      sku_key,
      (array_agg(manufacturer_name order by rnk))[1] as manufacturer_name,
      (array_agg(sku order by rnk))[1] as sku,
      (array_agg(clean_name order by rnk_name))[1] as name,
      (array_agg(description order by rnk) filter (where description is not null))[1] as description,
      (array_agg(firing_range order by rnk) filter (where firing_range is not null))[1] as firing_range,
      (array_agg(image_url order by rnk) filter (where image_url is not null))[1] as image_url,
      -- Family comes from a classifier per source, so the majority reading is
      -- steadier than any single shop's.
      mode() within group (order by family) as family,
      count(distinct source_id) as source_count,
      array_agg(distinct source_id) as sources,
      bool_or(from_maker) as has_maker_listing,
      max(last_seen_at) as last_seen_at
    from ranked
    group by manufacturer_id, sku_key
  ),
  upserted as (
    insert into catalogue.canonical_products (
      brand, manufacturer_sku, manufacturer_id, sku_key,
      name, family, description, firing_range, attributes, origin, updated_at
    )
    select
      m.manufacturer_name, m.sku, m.manufacturer_id, m.sku_key,
      m.name, m.family, m.description, m.firing_range,
      jsonb_strip_nulls(jsonb_build_object(
        'image_url', m.image_url,
        'promoted_from', jsonb_build_object(
          'sources', to_jsonb(m.sources),
          'source_count', m.source_count,
          'has_maker_listing', m.has_maker_listing,
          'last_seen_at', m.last_seen_at
        )
      )),
      'promoted', now()
    from merged m
    on conflict (manufacturer_id, sku_key)
      where manufacturer_id is not null and sku_key is not null
    do update set
      brand           = excluded.brand,
      manufacturer_sku = excluded.manufacturer_sku,
      name            = excluded.name,
      family          = coalesce(excluded.family, catalogue.canonical_products.family),
      description     = coalesce(excluded.description, catalogue.canonical_products.description),
      firing_range    = coalesce(excluded.firing_range, catalogue.canonical_products.firing_range),
      -- Merged, so a curator's own keys on a promoted row survive the next run.
      attributes      = catalogue.canonical_products.attributes || excluded.attributes,
      updated_at      = now()
    -- A row a person wrote is left exactly as they wrote it.
    where catalogue.canonical_products.origin = 'promoted'
    returning (xmax = 0) as inserted
  )
  select count(*) filter (where inserted),
         count(*) filter (where not inserted)
    into v_created, v_updated
    from upserted;

  -- Link every supplier row that resolves to a promoted identity, including the
  -- ones whose own fields lost the merge - the link is what makes them one
  -- product's competing offers rather than fifteen unrelated listings.
  with linked as (
    update catalogue.source_products sp
       set canonical_product_id = c.id
      from catalogue.manufacturer_aliases a
      join catalogue.manufacturers m on m.id = a.manufacturer_id and m.active
      join catalogue.canonical_products c on c.manufacturer_id = m.id
     where a.alias = lower(btrim(sp.brand))
       and c.sku_key = catalogue.sku_key(sp.manufacturer_sku)
       and sp.brand is not null
       and sp.canonical_product_id is distinct from c.id
       and (p_manufacturer is null or m.id = p_manufacturer)
    returning 1
  )
  select count(*) into v_linked from linked;

  select count(distinct manufacturer_id) into v_makers
    from catalogue.canonical_products
   where origin = 'promoted'
     and (p_manufacturer is null or manufacturer_id = p_manufacturer);

  update catalogue.catalogue_generation
     set generation = generation + 1, promoted_at = now()
   where singleton;

  return query select v_makers, v_created, v_updated, v_linked;
end;
$$;

-- 4. What the tenant sync reads ---------------------------------------------
--
-- One row per (canonical product, supplier variant) with that variant's latest
-- offer. This is the contract `mb_catalogue_sync` reads over the wire; nothing
-- in Odoo should ever query the loader's tables directly.
--
-- `unit_price` is per litre or kilogram and is for comparison only. Odoo's
-- product.supplierinfo wants the pack price against the pack unit of measure.

create or replace view catalogue.canonical_catalogue as
select
  c.id                  as canonical_product_id,
  c.manufacturer_id,
  c.brand,
  c.manufacturer_sku,
  c.name                as canonical_name,
  c.family,
  c.firing_range,
  c.attributes          as canonical_attributes,
  sp.id                 as source_product_id,
  sp.source_id,
  sp.parent_external_id,
  sp.name               as supplier_name,
  sp.supplier_reference,
  sp.product_url,
  sp.image_url,
  sp.availability,
  sp.last_seen_at,
  o.observed_at,
  o.price,
  o.currency,
  o.vat_status,
  o.quantity            as package_quantity,
  o.unit                as package_unit,
  o.unit_price,
  o.unit_price_per,
  o.last_seen_at        as offer_last_seen_at
from catalogue.canonical_products c
join catalogue.source_products sp
  on sp.canonical_product_id = c.id and sp.active
left join lateral (
  select o.observed_at, o.last_seen_at, o.price, o.currency, o.vat_status,
         o.quantity, o.unit, o.unit_price, o.unit_price_per
    from catalogue.offer_observations o
   where o.source_product_id = sp.id
   order by o.observed_at desc, o.id desc
   limit 1
) o on true
where c.active;

-- 5. The seed ---------------------------------------------------------------
--
-- Every name below was read off the loaded dump and judged one at a time. The
-- ones deliberately left out are as much a part of the curation as the ones
-- included: harry-ceradel, ceradel, les cousins, prodesco, taller gingell
-- barcelona, ulster ceramics pottery supplies, kettles pottery supplies,
-- penguin pottery and peter lavem are all shops, and several of them publish
-- article numbers that would pass for manufacturer codes.

insert into catalogue.manufacturers (id, name, homepage_url, source_id, notes)
values
  ('mayco', 'Mayco', 'https://www.maycocolors.com',
   (select id from catalogue.sources where id = 'mayco'),
   'Publishes specifications without prices; the identity rows in the dump are its own.'),
  ('amaco', 'AMACO', 'https://www.amaco.com',
   (select id from catalogue.sources where id = 'amaco'), null),
  ('botz', 'BOTZ', 'https://www.botz-glasuren.de', null, null),
  ('terracolor', 'Terracolor', 'https://www.terracolor.co.uk', null, null),
  ('spectrum', 'Spectrum Glazes', 'https://www.spectrumglazes.com',
   (select id from catalogue.sources where id = 'spectrum'), null),
  ('speedball', 'Speedball', 'https://www.speedballart.com',
   (select id from catalogue.sources where id = 'speedball'),
   'Owns AMACO; kept separate because the two publish separate code ranges.'),
  ('duncan', 'Duncan', 'https://www.duncanceramics.com', null, null),
  ('sio-2', 'SIO-2', 'https://www.sio-2.com',
   (select id from catalogue.sources where id = 'sio-2'), null),
  ('colorobbia', 'Colorobbia', 'https://www.colorobbia.com', null, null),
  ('gare', 'Gare', 'https://www.gareceramics.com', null, null),
  ('ferro', 'Ferro', 'https://www.ferro.com', null, null),
  ('laguna', 'Laguna Clay Company', 'https://www.lagunaclay.com', null, null),
  ('mason-color', 'Mason Color', 'https://www.masoncolor.com', null, null),
  ('carl-jaeger', 'Carl Jäger', 'https://www.carl-jaeger.de', null, null),
  ('goerg-schneider', 'Goerg & Schneider', 'https://www.goerg-schneider.de', null, null),
  ('sibelco', 'Sibelco', 'https://www.sibelco.com', null, null),
  ('chrysanthos', 'Chrysanthos', 'https://www.chrysanthos.com.au', null, null)
on conflict (id) do update
   set name = excluded.name,
       homepage_url = coalesce(excluded.homepage_url, catalogue.manufacturers.homepage_url),
       source_id = coalesce(excluded.source_id, catalogue.manufacturers.source_id),
       notes = coalesce(excluded.notes, catalogue.manufacturers.notes),
       updated_at = now();

insert into catalogue.manufacturer_aliases (alias, manufacturer_id)
values
  ('mayco', 'mayco'),
  ('mayco colors', 'mayco'),
  ('amaco', 'amaco'),
  ('botz', 'botz'),
  ('terracolor', 'terracolor'),
  ('terra color', 'terracolor'),
  ('spectrum', 'spectrum'),
  ('spectrum glazes', 'spectrum'),
  ('speedball', 'speedball'),
  ('speedball art', 'speedball'),
  ('duncan', 'duncan'),
  ('sio-2', 'sio-2'),
  ('sio2', 'sio-2'),
  ('colorobbia', 'colorobbia'),
  ('colorobbia art', 'colorobbia'),
  ('gare', 'gare'),
  ('ferro', 'ferro'),
  ('ferro frankfurt', 'ferro'),
  ('laguna', 'laguna'),
  ('laguna clay company', 'laguna'),
  ('mason color', 'mason-color'),
  ('carl jäger', 'carl-jaeger'),
  ('carl jaeger', 'carl-jaeger'),
  ('goerg & schneider', 'goerg-schneider'),
  ('g&s', 'goerg-schneider'),
  ('sibelco', 'sibelco'),
  ('chrysanthos', 'chrysanthos')
on conflict (alias) do update
   set manufacturer_id = excluded.manufacturer_id;

commit;
