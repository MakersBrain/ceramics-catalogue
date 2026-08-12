"""Writing a source's rows, and the rule that a bad run never destroys a good one.

`write_source` is where the pipeline is closest to losing data irrecoverably. If
an empty scrape overwrites a complete dump, the loader then reads a
complete-looking file, finds nothing in it, and retires the supplier's entire
catalogue. The dump is gone and so is the reason it went.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mb_ceramics_catalogue.crawl import artifacts

ROWS = [{"external_id": "s:1", "name": "Blue glaze"}, {"external_id": "s:2", "name": "Rouge"}]


def read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestWriteSource:
    def test_a_dump_is_written_as_ndjson(self, tmp_path: Path):
        artifact = artifacts.write_source(tmp_path, "ceradel", ROWS)
        assert artifact.status == "replaced"
        assert read(artifact.path) == ROWS

    def test_the_digest_and_size_describe_the_bytes_on_disk(self, tmp_path: Path):
        """`catalogue.jobs` records both, so they must be of the file, not of the
        records that were meant to be in it."""
        artifact = artifacts.write_source(tmp_path, "ceradel", ROWS)
        written = artifact.path.read_bytes()
        assert artifact.size == len(written)
        assert artifact.sha256 == hashlib.sha256(written).hexdigest()

    def test_an_empty_run_never_replaces_an_existing_dump(self, tmp_path: Path):
        """The rule the whole retirement chain depends on."""
        artifacts.write_source(tmp_path, "ceradel", ROWS)
        artifact = artifacts.write_source(tmp_path, "ceradel", [])
        assert artifact.status == "preserved_existing_nonempty"
        assert read(tmp_path / "ceradel.ndjson") == ROWS

    def test_an_empty_run_may_replace_when_explicitly_allowed(self, tmp_path: Path):
        artifacts.write_source(tmp_path, "ceradel", ROWS)
        artifact = artifacts.write_source(tmp_path, "ceradel", [], allow_empty=True)
        assert artifact.status == "replaced"
        assert read(tmp_path / "ceradel.ndjson") == []

    def test_an_empty_run_with_nothing_to_lose_writes_an_empty_file(self, tmp_path: Path):
        """No existing dump means no data at risk, so the file is created."""
        artifact = artifacts.write_source(tmp_path, "ceradel", [])
        assert artifact.status == "replaced"
        assert artifact.path.exists()

    def test_no_temporary_file_is_left_behind(self, tmp_path: Path):
        """The rename is what makes a killed process safe; a leftover `.tmp`
        would mean it did not happen."""
        artifacts.write_source(tmp_path, "ceradel", ROWS)
        assert list(tmp_path.glob("*.tmp")) == []


class TestWritePartial:
    def test_a_partial_is_written_beside_the_dump_never_as_it(self, tmp_path: Path):
        artifacts.write_source(tmp_path, "ceradel", ROWS)
        artifact = artifacts.write_partial(tmp_path, "ceradel", ROWS[:1])
        assert artifact.path.name == "ceradel.partial.ndjson"
        assert read(tmp_path / "ceradel.ndjson") == ROWS

    def test_an_empty_partial_writes_nothing(self, tmp_path: Path):
        artifact = artifacts.write_partial(tmp_path, "ceradel", [])
        assert artifact.status == "skipped_empty"
        assert not (tmp_path / "ceradel.partial.ndjson").exists()


class TestJobDirectory:
    def test_artifacts_are_namespaced_by_run_and_job(self, tmp_path: Path):
        """Two runs of one source must not write the same path.

        If they did, the second silently destroys the first's evidence and the
        `artifact_sha256` recorded against the first job stops describing
        anything that exists.
        """
        first = artifacts.job_directory(tmp_path, "run-a", "job-1")
        second = artifacts.job_directory(tmp_path, "run-b", "job-2")
        assert first != second
        artifacts.write_source(first, "ceradel", ROWS)
        artifacts.write_source(second, "ceradel", ROWS[:1])
        assert len(read(first / "ceradel.ndjson")) == 2
        assert len(read(second / "ceradel.ndjson")) == 1

    def test_the_directory_is_created_on_write(self, tmp_path: Path):
        target = artifacts.job_directory(tmp_path, "run-a", "job-1")
        assert not target.exists()
        artifacts.write_source(target, "ceradel", ROWS)
        assert target.is_dir()


class TestManifest:
    def test_it_carries_what_plan_load_reads(self, tmp_path: Path):
        """`write_status` and `truncated` are a contract with the loader."""
        manifest = artifacts.Manifest()
        manifest.record("ceradel", {"write_status": "replaced", "truncated": False, "records": 2})
        manifest.finish(2)
        path = manifest.write(tmp_path)

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["format"] == artifacts.DUMP_FORMAT
        assert data["record_count"] == 2
        assert data["sources"]["ceradel"]["write_status"] == "replaced"
        assert data["started_at"] and data["finished_at"]
