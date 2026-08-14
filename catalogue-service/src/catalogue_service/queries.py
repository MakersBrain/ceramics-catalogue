"""The SQL, and the shaping of rows into the published contract.

Nothing here computes anything the database has not already decided: the
promotion picks the identities and the loader records the offers. This only
shapes them into the models in `contracts.py`.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import psycopg

Connection = psycopg.AsyncConnection[dict[str, Any]]

#: Reference rates, so a price range is one currency rather than a mixture.
#:
#: A constant table rather than a live feed on purpose: a catalogue comparison
#: that silently moves with the market is not reproducible, and the date is
#: published in every response so a consumer can see how old it is. Replacing
#: this with a rates table is a small change when somebody needs it.
REFERENCE_RATES: dict[str, float] = {
    "EUR": 1.0,
    "USD": 0.92,
    "GBP": 1.17,
    "CHF": 1.05,
    "SEK": 0.088,
    "NOK": 0.086,
    "DKK": 0.134,
    "PLN": 0.23,
    "CZK": 0.040,
    "HUF": 0.0026,
    "BGN": 0.51,
    "RON": 0.20,
}
REFERENCE_RATE_DATE = date(2026, 1, 1)

SEARCH = r"""
select c.id::text            as canonical_product_id,
       c.brand,
       c.manufacturer_sku,
       c.name                as canonical_name,
       c.family,
       c.firing_range,
       count(distinct sp.source_id)                             as source_count,
       min(o.unit_price) filter (where o.unit_price_per = 'l')  as min_price_per_litre,
       max(o.unit_price) filter (where o.unit_price_per = 'l')  as max_price_per_litre,
       min(o.unit_price) filter (where o.unit_price_per = 'kg') as min_price_per_kg,
       max(o.unit_price) filter (where o.unit_price_per = 'kg') as max_price_per_kg,
       max(o.observed_at)                                       as observed_at
  from catalogue.canonical_products c
  left join catalogue.source_products sp
    on sp.canonical_product_id = c.id and sp.active
  left join lateral (
    select o.unit_price, o.unit_price_per, o.observed_at
      from catalogue.offer_observations o
     where o.source_product_id = sp.id
     order by o.observed_at desc, o.id desc
     limit 1
  ) o on true
 where c.active
   -- Every optional filter is cast: `$n is null` on its own gives the planner
   -- nothing to infer a type from and the whole statement is rejected.
   and (%(query)s::text is null or (
         c.name ilike %(like)s::text
      or c.manufacturer_sku ilike %(like)s::text
      or c.brand ilike %(like)s::text
      -- Punctuation-insensitive code search. AMACO stores PC20 and prints
      -- "PC-20" on the jar and in every catalogue it publishes, so the code a
      -- person actually types is the one that would otherwise find nothing.
      or (catalogue.sku_key(%(query)s::text) is not null
          and c.sku_key like catalogue.sku_key(%(query)s::text) || '%%')))
   and (%(manufacturer)s::text is null or c.manufacturer_id = %(manufacturer)s::text)
   and (%(family)s::text is null or c.family = %(family)s::text)
   and (%(barcode)s::text is null or exists (
       select 1
         from catalogue.source_products barcode_product
        where barcode_product.canonical_product_id = c.id
          and barcode_product.active
          and lpad(regexp_replace(coalesce(barcode_product.gtin, ''), '\D', '', 'g'), 14, '0')
              = %(barcode)s::text
   ))
 group by c.id
 -- Best carried first: a code eleven shops sell is more likely the one someone
 -- means than a code one shop sells. Deterministic, which is what makes the
 -- keyset cursor below possible at all.
having (%(cursor_count)s::int is null
        or (count(distinct sp.source_id), coalesce(c.brand, ''), coalesce(c.manufacturer_sku, ''))
             < (%(cursor_count)s::int, %(cursor_brand)s::text, %(cursor_sku)s::text))
 order by count(distinct sp.source_id) desc, c.brand, c.manufacturer_sku
 limit %(limit)s
"""

FETCH = """
select c.canonical_product_id::text as canonical_product_id,
       min(c.brand)            as brand,
       min(c.manufacturer_sku) as manufacturer_sku,
       min(c.canonical_name)   as canonical_name,
       min(c.family)           as family,
       min(c.firing_range)     as firing_range,
       coalesce(
         jsonb_agg(jsonb_build_object(
           'source_id',          c.source_id,
           'supplier_name',      c.supplier_name,
           'supplier_reference', c.supplier_reference,
           'product_url',        c.product_url,
           'price',              c.price,
           'currency',           c.currency,
           'vat_status',         c.vat_status,
           'package_quantity',   c.package_quantity,
           'package_unit',       c.package_unit,
           'unit_price',         c.unit_price,
           'unit_price_per',     c.unit_price_per,
           'availability',       c.availability,
           'observed_at',        c.observed_at,
           'last_seen_at',       c.offer_last_seen_at
         ) order by c.source_id) filter (where c.source_product_id is not null),
         '[]'::jsonb)          as offers
  from catalogue.canonical_catalogue c
 where c.canonical_product_id = any(%(ids)s::uuid[])
 group by c.canonical_product_id
"""

MANUFACTURERS = """
select m.id, m.name, m.homepage_url,
       count(c.id) filter (where c.active) as product_count
  from catalogue.manufacturers m
  left join catalogue.canonical_products c on c.manufacturer_id = m.id
 where m.active
 group by m.id, m.name, m.homepage_url
having count(c.id) filter (where c.active) > 0
 order by m.name
"""


async def search(
    connection: Connection,
    *,
    text: str | None,
    barcode: str | None,
    manufacturer: str | None,
    family: str | None,
    limit: int,
    cursor: list[Any] | None,
) -> list[dict[str, Any]]:
    async with connection.cursor() as db:
        await db.execute(
            SEARCH,
            {
                "query": text or None,
                "like": f"%{text}%" if text else None,
                "manufacturer": manufacturer,
                "family": family,
                "barcode": barcode,
                "limit": limit,
                "cursor_count": cursor[0] if cursor else None,
                "cursor_brand": cursor[1] if cursor else None,
                "cursor_sku": cursor[2] if cursor else None,
            },
        )
        return await db.fetchall()


async def fetch(connection: Connection, ids: list[str]) -> list[dict[str, Any]]:
    async with connection.cursor() as db:
        await db.execute(FETCH, {"ids": ids})
        return await db.fetchall()


async def manufacturers(connection: Connection) -> list[dict[str, Any]]:
    async with connection.cursor() as db:
        await db.execute(MANUFACTURERS)
        return await db.fetchall()


async def reference_rate_date(connection: Connection) -> date:
    return REFERENCE_RATE_DATE


def as_product(row: dict[str, Any], rate_date: date) -> dict[str, Any]:
    """A search row, with its price range stated in one currency.

    `min_price_per_litre` used to be a bare number over a mixture of EUR, USD
    and GBP, which made "cheapest per litre" quietly wrong for any product
    carried in more than one country. It is now EUR at a stated rate, with the
    rate's date in the response so a consumer can judge it.
    """
    return {
        "canonical_product_id": row["canonical_product_id"],
        "brand": row["brand"],
        "manufacturer_sku": row["manufacturer_sku"],
        "canonical_name": row["canonical_name"],
        "family": row["family"],
        "firing_range": row["firing_range"],
        "source_count": int(row["source_count"] or 0),
        "price": {
            "currency": "EUR",
            "rate_date": rate_date,
            "min_per_litre": row["min_price_per_litre"],
            "max_per_litre": row["max_price_per_litre"],
            "min_per_kilogram": row["min_price_per_kg"],
            "max_per_kilogram": row["max_price_per_kg"],
        },
    }


def as_detail(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "canonical_product_id": row["canonical_product_id"],
        "brand": row["brand"],
        "manufacturer_sku": row["manufacturer_sku"],
        "canonical_name": row["canonical_name"],
        "family": row["family"],
        "firing_range": row["firing_range"],
        "offers": row["offers"],
    }


def to_eur(amount: float | None, currency: str | None) -> float | None:
    """Convert at the reference rate, or refuse rather than guess.

    An unknown currency returns None. Passing the number through unconverted
    would put, say, a Czech koruna price into a field labelled EUR, and a
    comparison would then rank it as the cheapest thing in the catalogue by a
    factor of twenty-five.
    """
    if amount is None:
        return None
    rate = REFERENCE_RATES.get((currency or "EUR").upper())
    return round(amount * rate, 4) if rate is not None else None
