"""Test configuration shared by the suite.

There is deliberately no `sys.path` manipulation here any more. Phase 1 made
this a real `src`-layout package installed into the environment, so an import
resolves through the installed distribution — and if it ever stops doing so, the
tests should fail rather than be rescued by the working directory happening to
contain the modules. That silent rescue is exactly what §4.1 of the plan is about.

Database-backed tests need somewhere to run. Set `CATALOGUE_TEST_DSN` to a
PostgreSQL a test may **create and drop schemas in**; without it those tests
skip. It must not point at a database anyone cares about.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

SCHEMA = Path(__file__).resolve().parent.parent / "src" / "ateliera_catalogue" / "storage" / "schema"
EXTENSIONS = Path(__file__).resolve().parents[2] / "docker" / "initdb" / "00-extensions.sql"


def pytest_addoption(parser):
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="rewrite the frozen dumps from this run instead of comparing against them",
    )


def postgres_dsn() -> str | None:
    """Named without a `test_` prefix so pytest does not collect it as a test."""
    return os.environ.get("CATALOGUE_TEST_DSN") or None


requires_postgres = pytest.mark.skipif(
    not os.environ.get("CATALOGUE_TEST_DSN"),
    reason="set CATALOGUE_TEST_DSN to a throwaway PostgreSQL to run these",
)


@pytest.fixture
async def db() -> AsyncIterator:
    """A connection to a freshly created schema, dropped afterwards.

    Each test gets its own `catalogue` schema in its own connection's
    `search_path`, so tests can truncate freely and cannot see each other's
    rows. That is worth the setup cost: the queue tests are about what two
    workers do to the same rows, and a shared fixture would make every failure
    a question of ordering.
    """
    dsn = postgres_dsn()
    if not dsn:  # pragma: no cover - guarded by the marker
        pytest.skip("no CATALOGUE_TEST_DSN")

    import psycopg
    from psycopg.rows import dict_row

    async with await psycopg.AsyncConnection.connect(
        dsn, row_factory=dict_row, autocommit=True
    ) as connection:
        # Order matters, and getting it wrong is not obvious: pgcrypto is
        # installed *into* the catalogue schema, because `load_record` is
        # declared `set search_path = pg_catalog, catalogue` and would not
        # otherwise resolve `digest()`. Dropping the schema after creating the
        # extension therefore takes the extension with it, and every load then
        # fails with "function digest(bytea, unknown) does not exist".
        await connection.execute("drop schema if exists catalogue cascade")
        if EXTENSIONS.exists():
            await connection.execute(EXTENSIONS.read_text(encoding="utf-8"))
        # The same list the workers apply, imported rather than repeated: a
        # fixture that builds a different database from the deployment is a
        # fixture that passes while the deployment is broken. The promotion
        # file was missing here for exactly that reason.
        from ateliera_catalogue.storage.db import SCHEMA_FILES

        for name in SCHEMA_FILES:
            await connection.execute((SCHEMA / name).read_text(encoding="utf-8"))
        try:
            yield connection
        finally:
            await connection.execute("drop schema if exists catalogue cascade")
