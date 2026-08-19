from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mb_ceramics_catalogue.ops import database_transfer


def test_pg_tools_receive_connection_parts_without_a_dsn_in_argv(monkeypatch) -> None:
    captured = {}

    def run(command, **kwargs):
        captured.update(kwargs["env"])
        assert all("secret" not in item for item in command)

    monkeypatch.setattr(database_transfer.subprocess, "run", run)
    database_transfer._run(
        ["pg_dump"], dsn="postgresql://catalogue:secret@db.internal:5432/ateliera"
    )
    assert captured["PGHOST"] == "db.internal"
    assert captured["PGDATABASE"] == "ateliera"
    assert captured["PGPASSWORD"] == "secret"


def test_verify_rejects_a_changed_dump(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dump = tmp_path / "catalogue.dump"
    dump.write_bytes(b"valid")
    listing = tmp_path / "catalogue.dump.list"
    listing.write_bytes(b"list")
    manifest = {
        "format_version": 1,
        "dump": {
            "filename": dump.name,
            "size_bytes": dump.stat().st_size,
            "sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
            "list_filename": listing.name,
            "list_sha256": hashlib.sha256(listing.read_bytes()).hexdigest(),
            "ownership_and_acls_included": False,
            "globals_included": False,
        },
    }
    (tmp_path / "catalogue.dump.manifest.json").write_text(json.dumps(manifest))
    dump.write_bytes(b"changed")
    monkeypatch.setattr(database_transfer, "_run", lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match="checksum mismatch"):
        database_transfer.verify(dump, "pg_restore")


def test_restore_requires_database_bound_confirmation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="restore-empty:catalogue"):
        database_transfer.restore(tmp_path / "catalogue.dump", "pg_restore", "catalogue", "yes")


def test_empty_target_database_name_is_strict() -> None:
    with pytest.raises(ValueError, match="database name is invalid"):
        database_transfer._assert_empty_target("unused", "catalogue;drop database")
