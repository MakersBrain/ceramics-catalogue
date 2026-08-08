"""The master catalogue's read API.

Cross-tenant reference data, served to every tenant and owned by none of them.
A tenant searches the catalogue and fetches what its artisan chose. Nothing a
tenant does can change what another tenant sees.

**On the framework.** This module used to say it was "deliberately stdlib-only
apart from the driver … a framework would be more code to audit than the thing
it serves", and at the time that was right: it was a hand-rolled `do_GET` over
one view. The plan overrules it for a specific reason (§10.2). A spec generated
from a `BaseHTTPRequestHandler` is a spec written by hand and hoped to be true,
and six months later it is not. A published, verified contract is a different
requirement from "serve one view", and it is worth the dependency.

**The read-only property is stronger after the move, not weaker.** It used to be
the absence of a write path, which nothing enforced. It is now an assertion in
`tests/test_contract.py`: the generated document contains no operation other
than `get`, and the build fails if anyone adds one.
"""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from catalogue_service import queries
from catalogue_service.spec import registry

DSN = os.environ.get("CATALOGUE_DSN", "postgresql://catalogue:catalogue@postgres:5432/ateliera")
PORT = int(os.environ.get("CATALOGUE_PORT", "8686"))
MAX_LIMIT = 200
MAX_BATCH = 200
UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def problem(status: int, title: str, detail: str | None = None) -> Response:
    body: dict[str, Any] = {"type": "about:blank", "title": title, "status": status}
    if detail:
        body["detail"] = detail
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


def encode_cursor(row: dict[str, Any]) -> str:
    """The ordering key of the last row, opaque to the client.

    Base64 of the sort tuple rather than an offset: the ordering is
    `source_count desc, brand, manufacturer_sku`, and an offset would silently
    skip or repeat rows whenever a product gained or lost a stockist between
    pages.
    """
    key = [row["source_count"], row.get("brand") or "", row.get("manufacturer_sku") or ""]
    return base64.urlsafe_b64encode(json.dumps(key).encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> list[Any] | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:  # noqa: BLE001 - a bad cursor is a 400, not a 500
        return None
    return value if isinstance(value, list) and len(value) == 3 else None


async def health(request: Request) -> Response:
    try:
        async with request.app.state.pool.connection() as connection:
            await connection.execute("select 1")
    except psycopg.Error:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok"})


async def search(request: Request) -> Response:
    params = request.query_params

    # The old path answered both search and batch fetch depending on which
    # parameter was present, returning two different shapes from one operation
    # id. Kept working for one deprecation window, and pointed at its
    # replacement.
    if ids := params.get("ids"):
        return await _batch(request, ids, deprecated=True)

    try:
        limit = min(int(params.get("limit", 25)), MAX_LIMIT)
    except ValueError:
        return problem(400, "Bad Request", "limit must be a number")
    if limit < 1:
        return problem(400, "Bad Request", "limit must be positive")

    cursor = decode_cursor(params.get("cursor"))
    if params.get("cursor") and cursor is None:
        return problem(400, "Bad Request", "cursor is not a cursor this API issued")

    text = params.get("q")
    try:
        async with request.app.state.pool.connection() as connection:
            rows = await queries.search(
                connection,
                text=text,
                manufacturer=params.get("manufacturer"),
                family=params.get("family"),
                limit=limit + 1,
                cursor=cursor,
            )
            rate_date = await queries.reference_rate_date(connection)
    except psycopg.Error:
        return problem(503, "Service Unavailable", "the catalogue is not reachable")

    more = len(rows) > limit
    page = rows[:limit]
    return _json(
        {
            "products": [queries.as_product(row, rate_date) for row in page],
            "next_cursor": encode_cursor(page[-1]) if more and page else None,
        }
    )


async def fetch_one(request: Request) -> Response:
    identifier = request.path_params["id"]
    if not UUID_PATTERN.match(identifier):
        return problem(400, "Bad Request", "id must be a uuid")
    try:
        async with request.app.state.pool.connection() as connection:
            rows = await queries.fetch(connection, [identifier])
    except psycopg.Error:
        return problem(503, "Service Unavailable", "the catalogue is not reachable")
    if not rows:
        return problem(404, "Not Found", "no such canonical product")
    return _json(queries.as_detail(rows[0]))


async def batch(request: Request) -> Response:
    ids = request.query_params.get("ids")
    if not ids:
        return problem(400, "Bad Request", "ids is required")
    return await _batch(request, ids)


async def _batch(request: Request, ids: str, *, deprecated: bool = False) -> Response:
    wanted = [value.strip() for value in ids.split(",") if value.strip()]
    if not wanted:
        return problem(400, "Bad Request", "ids is empty")
    if len(wanted) > MAX_BATCH:
        return problem(400, "Bad Request", f"at most {MAX_BATCH} ids")
    # Validated rather than trusted: these become a uuid[] cast, and a bad value
    # should be a 400 and not a database error.
    if not all(UUID_PATTERN.match(value) for value in wanted):
        return problem(400, "Bad Request", "ids must be uuids")

    try:
        async with request.app.state.pool.connection() as connection:
            rows = await queries.fetch(connection, wanted)
    except psycopg.Error:
        return problem(503, "Service Unavailable", "the catalogue is not reachable")

    response = _json({"products": [queries.as_detail(row) for row in rows]})
    if deprecated:
        response.headers["Deprecation"] = "true"
        response.headers["Link"] = '</v1/canonical-products:batch>; rel="successor-version"'
    return response


async def manufacturers(request: Request) -> Response:
    try:
        async with request.app.state.pool.connection() as connection:
            rows = await queries.manufacturers(connection)
    except psycopg.Error:
        return problem(503, "Service Unavailable", "the catalogue is not reachable")
    return _json({"manufacturers": rows})


async def openapi(request: Request) -> Response:
    """The document this service is described by, served from the same code.

    Generated from the registry rather than read off disk, so it cannot be
    stale relative to the process serving it; CI separately checks that the
    checked-in file matches.
    """
    return _json(registry().build())


def _json(payload: Any) -> Response:
    return Response(json.dumps(payload, default=str), media_type="application/json")


def create_app(dsn: str = "") -> Starlette:
    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        pool: AsyncConnectionPool[psycopg.AsyncConnection[dict[str, Any]]] = AsyncConnectionPool(
            dsn or DSN,
            connection_class=psycopg.AsyncConnection[dict[str, Any]],
            min_size=1,
            max_size=8,
            kwargs={"row_factory": dict_row, "autocommit": True},
            open=False,
        )
        await pool.open(wait=True, timeout=30)
        app.state.pool = pool
        try:
            yield
        finally:
            await pool.close()

    return Starlette(
        routes=[
            Route("/health", health),
            Route("/openapi.json", openapi),
            # Before the parameterised route, or `:batch` is read as an id.
            Route("/v1/canonical-products:batch", batch),
            Route("/v1/canonical-products", search),
            Route("/v1/canonical-products/{id}", fetch_one),
            Route("/v1/manufacturers", manufacturers),
        ],
        lifespan=lifespan,
    )
