"""Test wiring for the control service.

Set `CATALOGUE_TEST_DSN` to a throwaway PostgreSQL a test may drop schemas in.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

SCHEMA = (
    Path(__file__).resolve().parents[2]
    / "catalogue-dump" / "src" / "mb_ceramics_catalogue" / "storage" / "schema"
)
EXTENSIONS = Path(__file__).resolve().parents[2] / "docker" / "initdb" / "00-extensions.sql"

TOKEN = "test-control-token"

requires_postgres = pytest.mark.skipif(
    not os.environ.get("CATALOGUE_TEST_DSN"),
    reason="set CATALOGUE_TEST_DSN to a throwaway PostgreSQL to run these",
)


def postgres_dsn() -> str | None:
    return os.environ.get("CATALOGUE_TEST_DSN") or None


async def build_schema(connection) -> None:
    # pgcrypto lives *inside* the catalogue schema because `load_record` is
    # declared `set search_path = pg_catalog, catalogue`; dropping the schema
    # after creating the extension takes the extension with it.
    await connection.execute("drop schema if exists catalogue cascade")
    if EXTENSIONS.exists():
        await connection.execute(EXTENSIONS.read_text(encoding="utf-8"))
    for name in (
        "catalogue-reference-schema.sql",
        "catalogue-reference-schema-v2.sql",
        "catalogue-ops-schema.sql",
    ):
        await connection.execute((SCHEMA / name).read_text(encoding="utf-8"))


@pytest.fixture
async def db() -> AsyncIterator:
    dsn = postgres_dsn()
    if not dsn:  # pragma: no cover
        pytest.skip("no CATALOGUE_TEST_DSN")

    import psycopg
    from psycopg.rows import dict_row

    async with await psycopg.AsyncConnection.connect(
        dsn, row_factory=dict_row, autocommit=True
    ) as connection:
        await build_schema(connection)
        try:
            yield connection
        finally:
            await connection.execute("drop schema if exists catalogue cascade")


@pytest.fixture
async def client(db, tmp_path: Path) -> AsyncIterator:
    """An HTTP client against the real app, with a real database behind it.

    Deliberately not a mocked pool: most of what is worth testing here is the
    SQL — what a cancel is allowed to touch, whether pressing a button twice is
    a no-op — and a mock would assert that the code calls itself.
    """
    import httpx

    from catalogue_control.app import create_app
    from catalogue_control.settings import Settings

    settings = Settings(
        dsn=postgres_dsn() or "", control_token=TOKEN, artifacts_dir=tmp_path
    )
    app = create_app(settings)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://control",
        headers={"authorization": f"Bearer {TOKEN}"},
    ) as http, app.router.lifespan_context(app):
        yield http
