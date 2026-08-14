"""Produce a reproducible performance/cost report without asserting live timings."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import psycopg
from psycopg.rows import dict_row

from mb_ceramics_catalogue.config.settings import Settings


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--dsn", default=None)
    command.add_argument("--run", type=UUID)
    command.add_argument("--base-url", default="http://127.0.0.1:5173")
    command.add_argument("--output", type=Path)
    return command


def _rows(connection: Any, query: str, params: Any = None) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(query, params)
        return list(cursor.fetchall())


def _timing(url: str) -> dict[str, Any]:
    readings = []
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        for _ in range(2):
            started = time.perf_counter()
            response = client.get(url, headers={"cache-control": "no-cache"})
            readings.append({
                "milliseconds": round((time.perf_counter() - started) * 1000, 3),
                "status": response.status_code,
                "bytes": len(response.content),
            })
    return {"cold": readings[0], "warm": readings[1]}


def report(dsn: str, run_id: UUID | None, base_url: str) -> dict[str, Any]:
    with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as connection:
        if run_id is None:
            found = _rows(
                connection,
                """select id from catalogue.runs
                    where status in ('complete', 'degraded') and finished_at is not null
                    order by finished_at desc limit 1""",
            )
            if not found:
                raise RuntimeError("no completed run is available")
            run_id = found[0]["id"]
        run = _rows(
            connection,
            """select id, status, started_at, finished_at,
                      extract(epoch from finished_at - started_at)::float8 wall_seconds
                 from catalogue.runs where id = %s""",
            (run_id,),
        )[0]
        jobs = _rows(
            connection,
            """select source_id, state,
                      extract(epoch from finished_at - started_at)::float8 seconds,
                      coalesce((summary->>'records')::bigint, 0) records,
                      coalesce((summary->>'requests')::bigint, 0) requests,
                      coalesce((summary->>'rendered_pages')::bigint, 0) renders,
                      coalesce((summary->>'error_count')::bigint, 0) errors,
                      coalesce((summary->>'http_tx_bytes_estimated')::bigint, 0)
                        + coalesce((summary->>'http_rx_bytes_estimated')::bigint, 0)
                        + coalesce((summary->>'browser_tx_bytes_estimated')::bigint, 0)
                        + coalesce((summary->>'browser_rx_bytes_estimated')::bigint, 0) bytes,
                      coalesce((summary->>'collection_seconds')::float8, 0) collection_seconds,
                      coalesce((summary->>'artifact_write_seconds')::float8, 0) write_seconds,
                      coalesce((summary->>'load_seconds')::float8, 0) load_seconds
                 from catalogue.jobs where run_id = %s order by source_id""",
            (run_id,),
        )
        relations = _rows(
            connection,
            """select relname,
                      pg_total_relation_size(c.oid) bytes,
                      pg_relation_size(c.oid) table_bytes,
                      pg_indexes_size(c.oid) index_bytes
                 from pg_class c join pg_namespace n on n.oid = c.relnamespace
                where n.nspname = 'catalogue' and c.relkind in ('r', 'm')
                order by pg_total_relation_size(c.oid) desc""",
        )
        database_bytes = _rows(
            connection, "select pg_database_size(current_database()) bytes"
        )[0]["bytes"]
        promotion = _rows(
            connection,
            """select payload from catalogue.event_log
                where run_id = %s and type = 'run.promoted' order by id desc limit 1""",
            (run_id,),
        )
    return {
        "run": {**run, "worker_hours": round(sum(row["seconds"] or 0 for row in jobs) / 3600, 4)},
        "phases": {
            key: round(sum(float(row[key]) for row in jobs), 6)
            for key in ("collection_seconds", "write_seconds", "load_seconds")
        } | {"promotion_seconds": (promotion[0]["payload"].get("promotion_seconds") if promotion else None)},
        "sources": jobs,
        "database": {"bytes": database_bytes, "relations": relations},
        "http": {path: _timing(base_url.rstrip("/") + path) for path in ("/", "/explore", "/ops")},
    }


def main() -> None:
    options = parser().parse_args()
    dsn = options.dsn or Settings().dsn
    if not dsn:
        raise SystemExit("set CATALOGUE_DSN or pass --dsn")
    payload = report(dsn, options.run, options.base_url)
    rendered = json.dumps(payload, default=str, indent=2, sort_keys=True) + "\n"
    if options.output:
        options.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
