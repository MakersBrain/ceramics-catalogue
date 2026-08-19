"""Create, verify, and restore guarded PostgreSQL catalogue transfers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

CRITICAL_TABLES = (
    "canonical_products",
    "source_products",
    "raw_records",
    "offer_observations",
    "runs",
    "jobs",
    "queue_outbox",
)
FORMAT_VERSION = 1


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _dsn() -> str:
    value = os.environ.get("CATALOGUE_DSN", "")
    if not value:
        raise ValueError("CATALOGUE_DSN is required")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str], *, dsn: str, stdout: Any = None) -> None:
    environment = os.environ.copy()
    # Individual libpq environment variables keep passwords out of process argv.
    if dsn:
        parameters = conninfo_to_dict(dsn)
        names = {
            "dbname": "PGDATABASE",
            "host": "PGHOST",
            "port": "PGPORT",
            "user": "PGUSER",
            "password": "PGPASSWORD",
            "sslmode": "PGSSLMODE",
            "sslrootcert": "PGSSLROOTCERT",
        }
        for source, target in names.items():
            if source in parameters and parameters[source] is not None:
                environment[target] = str(parameters[source])
    environment.setdefault("PGCONNECT_TIMEOUT", "15")
    subprocess.run(command, env=environment, stdout=stdout, check=True)


def inventory(dsn: str) -> dict[str, Any]:
    with psycopg.connect(dsn) as connection:
        row = connection.execute(
            "select current_database(), current_setting('server_version'), "
            "current_setting('server_version_num')::integer, pg_database_size(current_database())"
        ).fetchone()
        assert row is not None
        ledger = connection.execute(
            "select filename, applied_at from catalogue.schema_migrations order by applied_at, filename"
        ).fetchall()
        counts: dict[str, int] = {}
        for table in CRITICAL_TABLES:
            present = connection.execute(
                "select to_regclass(%s) is not null", (f"catalogue.{table}",)
            ).fetchone()
            if not present or not present[0]:
                raise ValueError(f"critical source table is missing: catalogue.{table}")
            count = connection.execute(
                sql.SQL("select count(*) from catalogue.{}").format(sql.Identifier(table))
            ).fetchone()
            assert count is not None
            counts[table] = count[0]
    return {
        "database": row[0],
        "postgres_version": row[1],
        "postgres_version_num": row[2],
        "database_size_bytes": row[3],
        "schema_migrations": [
            {"filename": filename, "applied_at": applied_at.isoformat()}
            for filename, applied_at in ledger
        ],
        "critical_table_counts": counts,
    }


def _paths(dump: Path) -> tuple[Path, Path]:
    return dump.with_suffix(dump.suffix + ".manifest.json"), dump.with_suffix(dump.suffix + ".list")


def create_dump(dump: Path, pg_dump: str, pg_restore: str) -> dict[str, Any]:
    manifest_path, list_path = _paths(dump)
    targets = (dump, manifest_path, list_path)
    if any(path.exists() for path in targets):
        raise ValueError("dump, manifest, and list outputs must not already exist")
    dump.parent.mkdir(parents=True, exist_ok=True)
    temporary = dump.with_suffix(dump.suffix + ".tmp")
    if temporary.exists():
        raise ValueError(f"temporary output already exists: {temporary}")
    dsn = _dsn()
    started_at = _now()
    before = inventory(dsn)
    try:
        _run(
            [pg_dump, "--format=custom", "--no-owner", "--no-acl", "--file", str(temporary)],
            dsn=dsn,
        )
        after = inventory(dsn)
        if before["schema_migrations"] != after["schema_migrations"] or before[
            "critical_table_counts"
        ] != after["critical_table_counts"]:
            raise ValueError("source changed during dump; quiesce writers and retry")
        with list_path.open("xb") as stream:
            _run([pg_restore, "--list", str(temporary)], dsn=dsn, stdout=stream)
        temporary.replace(dump)
        manifest = {
            "format_version": FORMAT_VERSION,
            "started_at": started_at,
            "completed_at": _now(),
            "source": after,
            "dump": {
                "filename": dump.name,
                "size_bytes": dump.stat().st_size,
                "sha256": _sha256(dump),
                "list_filename": list_path.name,
                "list_sha256": _sha256(list_path),
                "ownership_and_acls_included": False,
                "globals_included": False,
            },
        }
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return manifest
    except BaseException:
        temporary.unlink(missing_ok=True)
        if not dump.exists():
            list_path.unlink(missing_ok=True)
        raise


def verify(dump: Path, pg_restore: str) -> dict[str, Any]:
    manifest_path, list_path = _paths(dump)
    manifest = cast(dict[str, Any], json.loads(manifest_path.read_text(encoding="utf-8")))
    if manifest.get("format_version") != FORMAT_VERSION:
        raise ValueError("unsupported transfer manifest version")
    details = manifest.get("dump", {})
    if details.get("filename") != dump.name or details.get("list_filename") != list_path.name:
        raise ValueError("manifest filenames do not match the selected dump")
    if details.get("ownership_and_acls_included") is not False or details.get("globals_included") is not False:
        raise ValueError("transfer manifest does not exclude ownership, ACLs, and globals")
    if details.get("size_bytes") != dump.stat().st_size or details.get("sha256") != _sha256(dump):
        raise ValueError("dump size or checksum mismatch")
    if details.get("list_sha256") != _sha256(list_path):
        raise ValueError("restore-list checksum mismatch")
    _run([pg_restore, "--list", str(dump)], dsn="", stdout=subprocess.DEVNULL)
    return manifest


def _assert_empty_target(dsn: str, expected_database: str) -> None:
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,62}", expected_database):
        raise ValueError("expected target database name is invalid")
    with psycopg.connect(dsn) as connection:
        database = connection.execute("select current_database()").fetchone()
        if not database or database[0] != expected_database:
            raise ValueError("connected database does not match --expected-target-database")
        objects = connection.execute(
            "select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace "
            "where n.nspname not in ('pg_catalog', 'information_schema') "
            "and n.nspname !~ '^pg_toast' and c.relkind in ('r','p','v','m','S','f')"
        ).fetchone()
        if not objects or objects[0] != 0:
            raise ValueError("target database is not empty")


def restore(dump: Path, pg_restore: str, expected_database: str, confirmation: str) -> None:
    if confirmation != f"restore-empty:{expected_database}":
        raise ValueError(f"confirmation must be restore-empty:{expected_database}")
    manifest = verify(dump, pg_restore)
    dsn = _dsn()
    _assert_empty_target(dsn, expected_database)
    _run(
        [
            pg_restore,
            "--exit-on-error",
            "--single-transaction",
            "--no-owner",
            "--no-acl",
            "--dbname=",
            str(dump),
        ],
        dsn=dsn,
    )
    target = inventory(dsn)
    source = manifest["source"]
    if target["schema_migrations"] != source["schema_migrations"]:
        raise ValueError("restored migration ledger differs from source")
    if target["critical_table_counts"] != source["critical_table_counts"]:
        raise ValueError("restored critical-table counts differ from source")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    inventory_parser = subparsers.add_parser("inventory")
    inventory_parser.add_argument("--output", type=Path)
    dump_parser = subparsers.add_parser("dump")
    dump_parser.add_argument("dump", type=Path)
    dump_parser.add_argument("--pg-dump", default="pg_dump")
    dump_parser.add_argument("--pg-restore", default="pg_restore")
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("dump", type=Path)
    verify_parser.add_argument("--pg-restore", default="pg_restore")
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("dump", type=Path)
    restore_parser.add_argument("--pg-restore", default="pg_restore")
    restore_parser.add_argument("--expected-target-database", required=True)
    restore_parser.add_argument("--confirm", required=True)
    options = parser.parse_args()
    if options.command == "inventory":
        result = inventory(_dsn())
        rendered = json.dumps(result, indent=2) + "\n"
        if options.output:
            options.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
    elif options.command == "dump":
        print(json.dumps(create_dump(options.dump, options.pg_dump, options.pg_restore), indent=2))
    elif options.command == "verify":
        print(json.dumps(verify(options.dump, options.pg_restore), indent=2))
    else:
        restore(
            options.dump,
            options.pg_restore,
            options.expected_target_database,
            options.confirm,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
