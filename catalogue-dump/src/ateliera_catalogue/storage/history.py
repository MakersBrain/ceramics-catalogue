"""The SQLite price-history path, out of the crawl orchestrator.

`persist_history` was 84 lines of DDL and upsert logic living inside `dump.py` —
a whole storage backend inlined into the module that was supposed to be deciding
what to crawl. It moves here unchanged in behaviour.

It predates the PostgreSQL schema and is kept because the
`--history-db` flag and the files it wrote still exist. The price history that
matters now is in `catalogue.offer_observations`; this remains the answer for
someone who wants a single-file record of a run without a database to hand.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from ateliera_catalogue.scrapers.record import price_fingerprint

SCHEMA = """
pragma journal_mode = wal;
create table if not exists catalogue_import_runs (
  id text primary key, started_at text not null, finished_at text not null,
  source_count integer not null, record_count integer not null
);
create table if not exists catalogue_source_products (
  id integer primary key, source text not null, external_id text not null,
  parent_external_id text, name text, brand text, manufacturer_sku text,
  supplier_reference text, product_url text not null, image_url text,
  family text, availability text, first_seen_at text not null,
  last_seen_at text not null, unique(source, external_id)
);
create table if not exists catalogue_price_observations (
  id integer primary key,
  product_id integer not null references catalogue_source_products(id),
  observed_at text not null, price real, currency text, price_text text,
  vat_status text, quantity real, unit text, unit_price real, unit_price_per text,
  fingerprint text not null, raw_json text not null,
  unique(product_id, fingerprint)
);
create index if not exists catalogue_price_observations_product_time
  on catalogue_price_observations(product_id, observed_at);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def persist_history(
    database: str | Path, results: list[tuple[str, dict[str, Any]]]
) -> dict[str, int]:
    """Append price observations, suppressing an identical consecutive context.

    The `unique(product_id, fingerprint)` index is what does the suppressing: a
    daily run of a shop that changed nothing inserts no rows, so the table grows
    with price *changes* rather than with days.
    """
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    inserted = products = 0
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        finished = _now()
        total = sum(len(result["records"]) for _, result in results)
        connection.execute(
            "insert into catalogue_import_runs values (?, ?, ?, ?, ?)",
            (str(uuid4()), finished, finished, len(results), total),
        )
        for source, result in results:
            for record in result["records"]:
                stamp = record["fetched_at"]
                package = record.get("package_size") or {}
                unit_price = record.get("unit_price") or {}
                row = connection.execute(
                    "select id from catalogue_source_products where source = ? and external_id = ?",
                    (source, record["external_id"]),
                ).fetchone()
                identity = (
                    record["name"], record.get("brand"), record.get("manufacturer_sku"),
                    record.get("supplier_reference"), record["product_url"],
                    record.get("image_url"), record.get("family"), record.get("availability"),
                )
                if row:
                    product_id = row[0]
                    connection.execute(
                        "update catalogue_source_products set name=?, brand=?, manufacturer_sku=?,"
                        " supplier_reference=?, product_url=?, image_url=?, family=?, availability=?,"
                        " last_seen_at=? where id=?",
                        (*identity, stamp, product_id),
                    )
                else:
                    product_id = connection.execute(
                        "insert into catalogue_source_products (source, external_id, parent_external_id,"
                        " name, brand, manufacturer_sku, supplier_reference, product_url, image_url,"
                        " family, availability, first_seen_at, last_seen_at)"
                        " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) returning id",
                        (source, record["external_id"], record.get("parent_external_id"), *identity, stamp, stamp),
                    ).fetchone()[0]
                    products += 1
                if record.get("price") is None:
                    continue
                cursor = connection.execute(
                    "insert or ignore into catalogue_price_observations (product_id, observed_at, price,"
                    " currency, price_text, vat_status, quantity, unit, unit_price, unit_price_per,"
                    " fingerprint, raw_json) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        product_id, stamp, record["price"], record.get("currency"), record.get("price_text"),
                        record.get("vat_status"), package.get("value"), package.get("unit"),
                        unit_price.get("value"), unit_price.get("per"),
                        price_fingerprint(record), json.dumps(record.get("raw"), ensure_ascii=False, default=str),
                    ),
                )
                inserted += cursor.rowcount
    return {"new_products": products, "new_price_observations": inserted}
