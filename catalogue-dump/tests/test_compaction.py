"""Legacy duplicate compaction preserves state transitions and references."""

import psycopg
import pytest
from psycopg.rows import dict_row

from mb_ceramics_catalogue.ops import compaction
from mb_ceramics_catalogue.storage import postgres

from .conftest import postgres_dsn, requires_postgres
from .test_loader import at, record

pytestmark = [pytest.mark.postgres, requires_postgres]


def test_compaction_collapses_only_consecutive_states_and_semantic_raw_duplicates(db):
    dsn = postgres_dsn()
    assert dsn
    with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as connection:
        postgres.ensure_staging(connection)
        first = record("1", "Blue", 10)
        postgres.load_source(connection, "ceradel", [first], whole=False)
        # Simulate the legacy loader's daily duplicate A.
        connection.execute(
            """insert into catalogue.offer_observations
               (source_product_id, raw_record_id, observed_at, last_seen_at, price, currency,
                vat_status, availability, context_sha256, attributes)
               select source_product_id, raw_record_id, observed_at + interval '1 day',
                      observed_at + interval '1 day', price, currency, vat_status, availability,
                      context_sha256, attributes
                 from catalogue.offer_observations"""
        )
        postgres.load_source(
            connection, "ceradel", [at(first, "2026-08-10T00:00:00Z", price=12)], whole=False
        )
        # Legacy duplicate B, then a real return to A.
        connection.execute(
            """insert into catalogue.offer_observations
               (source_product_id, raw_record_id, observed_at, last_seen_at, price, currency,
                vat_status, availability, context_sha256, attributes)
               select source_product_id, raw_record_id, observed_at + interval '1 day',
                      observed_at + interval '1 day', price, currency, vat_status, availability,
                      context_sha256, attributes
                 from catalogue.offer_observations where price = 12"""
        )
        postgres.load_source(
            connection, "ceradel", [at(first, "2026-08-12T00:00:00Z", price=10)], whole=False
        )
        # A legacy raw duplicate with a volatile fetched_at and therefore a
        # different old hash, but identical semantic content.
        connection.execute(
            """insert into catalogue.raw_records
               (source_product_id, fetched_at, first_seen_at, last_seen_at, record_sha256, record)
               select source_product_id, fetched_at + interval '1 hour',
                      first_seen_at + interval '1 hour', last_seen_at + interval '1 hour',
                      catalogue.digest(convert_to(record::text || 'legacy', 'UTF8'), 'sha256'),
                      jsonb_set(record, '{fetched_at}', to_jsonb('2026-08-08T01:00:00Z'::text))
                 from catalogue.raw_records order by id limit 1"""
        )

        preview = compaction.compact(connection, 100, execute=False)
        assert preview.raw_deleted >= 1
        assert preview.offers_deleted == 2
        executed = compaction.compact(connection, 100, execute=True)
        assert executed.raw_deleted == preview.raw_deleted
        assert executed.offers_deleted == preview.offers_deleted
        prices = connection.execute(
            "select price::float8 price from catalogue.offer_observations order by observed_at"
        ).fetchall()
        assert [row["price"] for row in prices] == [10, 12, 10]
        assert compaction.compact(connection, 100, execute=False) == compaction.CompactReport()
