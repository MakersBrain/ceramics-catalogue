"""The in-process loader, against a real PostgreSQL.

`plan_load` decides *whether* a file may be grounds for retirement and is tested
without a database in test_load_planning.py. This is the other half: given that
decision, does the load actually retire the right rows, in one transaction, and
does a bad record cost one source rather than the whole load.

Retirement is the dangerous operation in this codebase. Marking a live catalogue
withdrawn is invisible until somebody notices a supplier mysteriously stopped
stocking anything, so it is worth testing at the level where it really happens.
"""

from __future__ import annotations

import json
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from ateliera_catalogue.storage import postgres

from .conftest import postgres_dsn, requires_postgres

pytestmark = [pytest.mark.postgres, requires_postgres]

SOURCES = {
    "ceradel": {"label": "Ceradel", "url": "https://ceradel.fr/", "country": "FR", "scope": "materials"},
}


def record(external: str, name: str, price: float = 12.5) -> dict:
    """A minimal `ceramics.catalogue_item.v2` row the loader will accept."""
    return {
        "format": "ceramics.catalogue_item.v2",
        "source": "ceradel",
        "external_id": f"ceradel:{external}",
        "parent_external_id": f"ceradel:{external}",
        "product_url": f"https://ceradel.fr/p/{external}",
        "extraction_method": "api_json",
        "source_detail_level": "api",
        "fetched_at": "2026-08-08T00:00:00Z",
        "name": name,
        "name_raw": name,
        "price": price,
        "currency": "EUR",
        "vat_status": "inclusive",
    }


@pytest.fixture
def sync_db(db):
    """A synchronous connection to the schema the async `db` fixture built.

    The loader is synchronous — it is called from a worker's thread pool and
    from a CLI, neither of which needs it to be async — so it needs its own
    connection rather than the async one the fixture yields.
    """
    dsn = postgres_dsn()
    assert dsn is not None
    with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as connection:
        yield connection


def active_products(connection) -> dict[str, bool]:
    with connection.cursor() as cursor:
        cursor.execute("select external_id, active from catalogue.source_products order by external_id")
        return {row["external_id"]: row["active"] for row in cursor.fetchall()}


class TestLoadSource:
    def test_records_become_source_products(self, sync_db):
        postgres.ensure_staging(sync_db)
        report = postgres.load_source(
            sync_db, "ceradel", [record("1", "Blue glaze"), record("2", "Red glaze")], whole=True
        )
        assert report.records == 2
        assert report.ok
        assert set(active_products(sync_db)) == {"ceradel:1", "ceradel:2"}

    def test_a_whole_dump_retires_what_it_no_longer_lists(self, sync_db):
        """A product that was there last time and is absent now was withdrawn."""
        postgres.ensure_staging(sync_db)
        postgres.load_source(
            sync_db, "ceradel", [record("1", "Blue"), record("2", "Red")], whole=True
        )
        report = postgres.load_source(sync_db, "ceradel", [record("1", "Blue")], whole=True)

        assert report.retired == 1
        assert active_products(sync_db) == {"ceradel:1": True, "ceradel:2": False}

    def test_a_partial_dump_only_ever_adds(self, sync_db):
        """Retiring against an unfinished run withdraws products still for sale."""
        postgres.ensure_staging(sync_db)
        postgres.load_source(
            sync_db, "ceradel", [record("1", "Blue"), record("2", "Red")], whole=True
        )
        report = postgres.load_source(sync_db, "ceradel", [record("1", "Blue")], whole=False)

        assert report.retired == 0
        assert active_products(sync_db) == {"ceradel:1": True, "ceradel:2": True}

    def test_retirement_is_scoped_to_one_source(self, sync_db):
        """Loading one shop must not withdraw another's catalogue."""
        postgres.ensure_staging(sync_db)
        other = {**record("9", "Other shop"), "source": "mayco", "external_id": "mayco:9",
                 "parent_external_id": "mayco:9"}
        postgres.load_source(sync_db, "mayco", [other], whole=True)
        postgres.load_source(sync_db, "ceradel", [record("1", "Blue")], whole=True)
        postgres.load_source(sync_db, "ceradel", [], whole=True)

        products = active_products(sync_db)
        assert products["mayco:9"] is True

    def test_a_retired_product_comes_back_when_the_shop_lists_it_again(self, sync_db):
        postgres.ensure_staging(sync_db)
        postgres.load_source(sync_db, "ceradel", [record("1", "Blue"), record("2", "Red")], whole=True)
        postgres.load_source(sync_db, "ceradel", [record("1", "Blue")], whole=True)
        assert active_products(sync_db)["ceradel:2"] is False

        postgres.load_source(sync_db, "ceradel", [record("1", "Blue"), record("2", "Red")], whole=True)
        assert active_products(sync_db)["ceradel:2"] is True

    def test_staging_is_left_empty(self, sync_db):
        """A source that leaves rows staged would have the next one retire
        against the union of both."""
        postgres.ensure_staging(sync_db)
        postgres.load_source(sync_db, "ceradel", [record("1", "Blue")], whole=True)
        with sync_db.cursor() as cursor:
            cursor.execute("select count(*) as n from import_staging")
            assert cursor.fetchone()["n"] == 0

    def test_a_bad_record_rolls_the_whole_source_back(self, sync_db):
        """Either all four steps happen for a source or none do.

        The `psql` version could be interrupted between staging, loading,
        retiring and truncating, leaving a source counted as loaded with its
        retirement half-applied.
        """
        postgres.ensure_staging(sync_db)
        postgres.load_source(sync_db, "ceradel", [record("1", "Blue"), record("2", "Red")], whole=True)

        broken = record("3", "Broken")
        del broken["product_url"]  # not null in catalogue.source_products

        with pytest.raises(psycopg.Error):
            postgres.load_source(sync_db, "ceradel", [record("1", "Blue"), broken], whole=True)

        # Nothing retired, nothing added, staging clean.
        assert active_products(sync_db) == {"ceradel:1": True, "ceradel:2": True}
        with sync_db.cursor() as cursor:
            cursor.execute("select count(*) as n from import_staging")
            assert cursor.fetchone()["n"] == 0

    def test_the_retire_statement_takes_the_source_as_a_parameter(self, sync_db):
        """It used to be `RETIRE.replace("%(source)s", f"\'{source}\'")`.

        Source ids come from a checked-in file, so this was never an injection
        surface in practice — but it blocked running the load in-process and it
        was one careless caller away from being one. Passing a quote-laden name
        now reaches the database as a value that matches nothing, rather than as
        SQL that ends the string and starts a new statement.

        (`catalogue.sources` separately constrains ids to `^[a-z0-9][a-z0-9-]*$`,
        so this exercises the statement directly: the point under test is the
        parameterisation, not the check constraint behind it.)
        """
        postgres.ensure_staging(sync_db)
        postgres.load_source(sync_db, "ceradel", [record("1", "Blue")], whole=True)

        hostile = "ceradel'; drop table catalogue.source_products; --"
        with sync_db.cursor() as cursor:
            cursor.execute(postgres.RETIRE, {"source": hostile})
            assert cursor.rowcount == 0

        # The table is still there, and ceradel's own row was not touched.
        assert active_products(sync_db) == {"ceradel:1": True}


class TestLoadDump:
    def write(self, directory: Path, source: str, rows: list[dict], partial: bool = False) -> None:
        suffix = ".partial.ndjson" if partial else ".ndjson"
        (directory / f"{source}{suffix}").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )

    def test_a_directory_loads_and_reports(self, sync_db, tmp_path):
        self.write(tmp_path, "ceradel", [record("1", "Blue"), record("2", "Red")])
        plans, _ = postgres.plan_load(tmp_path)
        report = postgres.load_dump(sync_db, plans, SOURCES, {"ceradel"})

        assert report.ok
        assert report.records == 2
        assert report.run_id is not None
        assert report.products == 2

    def test_one_bad_source_does_not_cost_the_others(self, sync_db, tmp_path):
        """A defect in the third of sixty-three sources used to cost the other sixty."""
        broken = record("9", "Broken")
        del broken["product_url"]
        self.write(tmp_path, "ceradel", [record("1", "Blue")])
        self.write(tmp_path, "mayco", [broken])
        plans, _ = postgres.plan_load(tmp_path)

        report = postgres.load_dump(sync_db, plans, SOURCES, {"ceradel", "mayco"})

        assert [source.source for source in report.loaded] == ["ceradel"]
        assert [source.source for source in report.failures] == ["mayco"]
        assert not report.ok
        assert report.products == 1

    def test_the_import_run_records_the_outcome(self, sync_db, tmp_path):
        self.write(tmp_path, "ceradel", [record("1", "Blue")])
        plans, _ = postgres.plan_load(tmp_path)
        report = postgres.load_dump(sync_db, plans, SOURCES, {"ceradel"})

        with sync_db.cursor() as cursor:
            cursor.execute(
                "select status, record_count from catalogue.import_runs where id = %s",
                (report.run_id,),
            )
            row = cursor.fetchone()
        assert row["status"] == "complete"
        assert row["record_count"] == 1

    def test_a_failed_load_marks_the_import_run_failed(self, sync_db, tmp_path):
        broken = record("9", "Broken")
        del broken["product_url"]
        self.write(tmp_path, "ceradel", [broken])
        plans, _ = postgres.plan_load(tmp_path)
        report = postgres.load_dump(sync_db, plans, SOURCES, {"ceradel"})

        with sync_db.cursor() as cursor:
            cursor.execute("select status from catalogue.import_runs where id = %s", (report.run_id,))
            assert cursor.fetchone()["status"] == "failed"

    def test_keep_stale_suppresses_retirement(self, sync_db, tmp_path):
        self.write(tmp_path, "ceradel", [record("1", "Blue"), record("2", "Red")])
        plans, _ = postgres.plan_load(tmp_path)
        postgres.load_dump(sync_db, plans, SOURCES, {"ceradel"})

        self.write(tmp_path, "ceradel", [record("1", "Blue")])
        plans, _ = postgres.plan_load(tmp_path)
        postgres.load_dump(sync_db, plans, SOURCES, {"ceradel"}, keep_stale=True)

        assert active_products(sync_db)["ceradel:2"] is True

    def test_sources_are_described_from_the_configuration(self, sync_db, tmp_path):
        """`load_record` creates a source row from the id alone; the label, shop
        URL and country come from sources.json."""
        self.write(tmp_path, "ceradel", [record("1", "Blue")])
        plans, _ = postgres.plan_load(tmp_path)
        postgres.load_dump(sync_db, plans, SOURCES, {"ceradel"})

        with sync_db.cursor() as cursor:
            cursor.execute("select label, homepage_url, metadata from catalogue.sources where id='ceradel'")
            row = cursor.fetchone()
        assert row["label"] == "Ceradel"
        assert row["homepage_url"] == "https://ceradel.fr/"
        assert row["metadata"]["country"] == "FR"

    def test_the_load_is_traceable_to_the_crawl_that_produced_it(self, sync_db, tmp_path):
        from ateliera_catalogue.ops import runs as ops_runs

        with sync_db.cursor() as cursor:
            cursor.execute(
                "insert into catalogue.runs (kind, status) values ('manual', 'running') returning id"
            )
            run_id = cursor.fetchone()["id"]
        del ops_runs

        self.write(tmp_path, "ceradel", [record("1", "Blue")])
        plans, _ = postgres.plan_load(tmp_path)
        report = postgres.load_dump(sync_db, plans, SOURCES, {"ceradel"}, run_id=run_id)

        with sync_db.cursor() as cursor:
            cursor.execute("select run_id from catalogue.import_runs where id = %s", (report.run_id,))
            assert cursor.fetchone()["run_id"] == run_id


class TestConcurrency:
    def test_two_connections_stage_independently(self, sync_db):
        """The reason staging is a temp table rather than a shared unlogged one.

        Two workers loading at once into one table would each retire against the
        union of both dumps — which, for two sources, withdraws everything
        neither of them happened to list.
        """
        dsn = postgres_dsn()
        assert dsn is not None
        postgres.ensure_staging(sync_db)

        with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as other:
            postgres.ensure_staging(other)
            with sync_db.cursor() as cursor:
                cursor.execute(
                    "insert into import_staging (source_file, record) values ('a', '{}'::jsonb)"
                )
            with other.cursor() as cursor:
                cursor.execute("select count(*) as n from import_staging")
                assert cursor.fetchone()["n"] == 0, "one connection saw the other's staged rows"
