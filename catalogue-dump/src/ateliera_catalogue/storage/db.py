"""Connecting to the catalogue database.

Thin on purpose. The interesting decisions are all in the SQL, and a layer that
hid the SQL would make the queue's `for update skip locked` harder to read
rather than easier.

Two things it does insist on:

* **`row_factory=dict_row` everywhere.** Positional tuples are how a column
  added in the middle of a `select *` silently shifts every reader.
* **`autocommit=True` on the pool.** Every write path here is either a single
  statement or an explicit `async with conn.transaction()`, and an implicit
  open transaction on a pooled connection is how a worker ends up holding row
  locks it forgot about while it waits for a shop to answer.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from ateliera_catalogue.observability import logging as obs

LOGGER = obs.get_logger("catalogue.db")


def dsn_from_environment(explicit: str = "") -> str:
    """The connection string, from the argument or the environment."""
    found = (
        explicit
        or os.environ.get("CATALOGUE_DSN", "")
        or os.environ.get("DATABASE_URL", "")
    )
    if not found:
        raise ValueError(
            "no database connection string; pass --dsn or set CATALOGUE_DSN"
        )
    return found


@asynccontextmanager
async def connect(dsn: str) -> AsyncIterator[psycopg.AsyncConnection[dict[str, Any]]]:
    """One connection, for a command that makes a handful of statements."""
    async with await psycopg.AsyncConnection.connect(
        dsn, row_factory=dict_row, autocommit=True
    ) as connection:
        yield connection


#: A pool whose connections are known to yield dict rows. Spelling this out is
#: what stops every `async with pool.connection()` from looking like a tuple-row
#: connection to a type checker, and then needing a cast at each of forty sites.
DictPool = AsyncConnectionPool[psycopg.AsyncConnection[dict[str, Any]]]


@asynccontextmanager
async def pool(dsn: str, *, minimum: int = 1, maximum: int = 4) -> AsyncIterator[DictPool]:
    """A pool, for a long-lived process.

    The worker needs at least two connections at once — one for the job it is
    running and one for the heartbeat that must keep heartbeating while that job
    holds its connection busy — so a pool is not an optimisation here, it is
    what stops the liveness signal from being blocked by the work it reports on.
    """
    connection_pool: DictPool = AsyncConnectionPool(
        dsn,
        connection_class=psycopg.AsyncConnection[dict[str, Any]],
        min_size=minimum,
        max_size=maximum,
        kwargs={"row_factory": dict_row, "autocommit": True},
        open=False,
    )
    await connection_pool.open(wait=True, timeout=30)
    try:
        yield connection_pool
    finally:
        await connection_pool.close()


async def fetch_all(
    connection: psycopg.AsyncConnection[dict[str, Any]], sql: str, params: Any = None
) -> list[dict[str, Any]]:
    async with connection.cursor() as cursor:
        await cursor.execute(sql, params)
        return await cursor.fetchall()


async def fetch_one(
    connection: psycopg.AsyncConnection[dict[str, Any]], sql: str, params: Any = None
) -> dict[str, Any] | None:
    async with connection.cursor() as cursor:
        await cursor.execute(sql, params)
        return await cursor.fetchone()


async def execute(
    connection: psycopg.AsyncConnection[dict[str, Any]], sql: str, params: Any = None
) -> int:
    async with connection.cursor() as cursor:
        await cursor.execute(sql, params)
        return cursor.rowcount


def schema_directory() -> Any:
    from pathlib import Path

    return Path(__file__).resolve().parent / "schema"


#: The schema files, in the order they have to run.
#:
#: Spelled out rather than globbed, because the order is load-bearing and
#: alphabetical is not it: `catalogue-canonical-promotion.sql` sorts first and
#: alters `catalogue.canonical_products`, which the reference schema three
#: entries below is what creates. Globbing worked only for as long as the names
#: happened to sort correctly, and stopped the day the promotion file was added.
#:
#: `docker-compose.yml` mounts the same files into initdb under numeric
#: prefixes for the same reason. Both lists have to agree.
SCHEMA_FILES = (
    "catalogue-reference-schema.sql",
    "catalogue-reference-schema-v2.sql",
    "catalogue-ops-schema.sql",
    "catalogue-canonical-promotion.sql",
)


async def apply_schema(connection: psycopg.AsyncConnection[dict[str, Any]]) -> list[str]:
    """Apply every schema file in dependency order, and say which ran.

    Every file is written to be re-runnable (`create table if not exists`,
    `add column if not exists`), so this is safe against a database that already
    has some of them — which is the normal case, since the reference schema is
    applied by initdb and the ops schema arrived later.
    """
    directory = schema_directory()
    applied = []
    for name in SCHEMA_FILES:
        path = directory / name
        await connection.execute(path.read_text(encoding="utf-8"))
        applied.append(name)
        LOGGER.info("schema.applied", file=name)
    return applied
