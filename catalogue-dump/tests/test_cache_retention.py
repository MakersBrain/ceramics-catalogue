from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from mb_ceramics_catalogue.ops import retention
from mb_ceramics_catalogue.scrapers.cache import prune

from .conftest import postgres_dsn, requires_postgres


def entry(root: Path, name: str, size: int, accessed: float) -> Path:
    path = root / "host" / "aa" / f"{name}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (accessed, accessed))
    return path


def test_cache_prune_dry_run_matches_execution(tmp_path: Path) -> None:
    old = entry(tmp_path, "old", 10, 10)
    large = entry(tmp_path, "large", 30, 90)
    keep = entry(tmp_path, "keep", 10, 100)

    preview = prune(tmp_path, max_age_seconds=50, max_bytes=15, now=100, dry_run=True)
    executed = prune(tmp_path, max_age_seconds=50, max_bytes=15, now=100, dry_run=False)

    assert preview.paths == executed.paths == (large, old)
    assert keep.exists()
    assert not old.exists() and not large.exists()


def test_cache_prune_preserves_excluded_fixture_tree(tmp_path: Path) -> None:
    frozen = entry(tmp_path / "golden", "fixture", 20, 1)
    report = prune(
        tmp_path,
        max_age_seconds=1,
        max_bytes=0,
        now=100,
        dry_run=False,
        exclude=(tmp_path / "golden",),
    )
    assert report.files == 0
    assert frozen.exists()


@pytest.mark.postgres
@requires_postgres
def test_artifact_retention_selection_matches_execution_and_protects_shared_paths(db, tmp_path):
    dsn = postgres_dsn()
    assert dsn
    old = tmp_path / "old.ndjson.gz"
    shared = tmp_path / "shared.ndjson.gz"
    old.write_bytes(b"old")
    shared.write_bytes(b"shared")
    with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as connection:
        rows = [
            ("old", old, "20 days"),
            ("middle", shared, "10 days"),
            ("new", shared, "1 day"),
        ]
        ids = {}
        for name, path, age in rows:
            run_id = uuid4()
            job_id = uuid4()
            ids[name] = job_id
            connection.execute(
                "insert into catalogue.runs(id, kind, status) values (%s, 'manual', 'complete')",
                (run_id,),
            )
            connection.execute(
                """insert into catalogue.jobs
                   (id, run_id, source_id, host, state, finished_at, artifact_path)
                   values (%s, %s, 'shop', 'shop.test', 'succeeded', now() - %s::interval, %s)""",
                (job_id, run_id, age, str(path)),
            )
        preview = retention.select(connection, tmp_path)
        assert [target.job_id for target in preview.targets] == [str(ids["old"])]
        retention.execute(connection, preview)
        assert not old.exists()
        assert shared.exists()
        assert retention.select(connection, tmp_path).files == 0
        row = connection.execute(
            "select artifact_path, summary from catalogue.jobs where id = %s", (ids["old"],)
        ).fetchone()
        assert row["artifact_path"] is None
        assert row["summary"]["artifact_unavailable"] is True
