from __future__ import annotations

import gzip
import hashlib
import json
from typing import Any

import pytest

from catalogue_control.changes import ArtifactError, compare, read_artifact


def test_compare_reports_added_removed_and_field_changes() -> None:
    before: dict[str, dict[str, Any]] = {
        "shop:a": {"external_id": "shop:a", "name": "A", "price": 10, "fetched_at": "old"},
        "shop:b": {"external_id": "shop:b", "name": "B"},
        "shop:same": {"external_id": "shop:same", "name": "Same", "fetched_at": "old"},
    }
    after: dict[str, dict[str, Any]] = {
        "shop:a": {"external_id": "shop:a", "name": "A", "price": 12, "fetched_at": "new"},
        "shop:c": {"external_id": "shop:c", "name": "C"},
        "shop:same": {"external_id": "shop:same", "name": "Same", "fetched_at": "new"},
    }

    result = compare(before, after)

    assert {key: result[key] for key in ("added", "removed", "changed", "unchanged")} == {
        "added": 1,
        "removed": 1,
        "changed": 1,
        "unchanged": 1,
    }
    changed = next(item for item in result["items"] if item["kind"] == "changed")
    assert changed["fields"] == [{"field": "price", "before": 10, "after": 12}]


def test_compare_filters_and_bounds_the_returned_items() -> None:
    after = {
        f"shop:{number}": {"external_id": f"shop:{number}", "name": f"Bowl {number}"}
        for number in range(5)
    }
    result = compare({}, after, kind="added", search="bowl", limit=2)
    assert result["added"] == 5
    assert result["matched"] == 5
    assert len(result["items"]) == 2


def test_read_artifact_checks_root_checksum_and_duplicate_ids(tmp_path) -> None:
    artifact = tmp_path / "source.ndjson"
    body = b'{"external_id":"one","name":"One"}\n'
    artifact.write_bytes(body)
    assert read_artifact(tmp_path, "source.ndjson", hashlib.sha256(body).hexdigest())["one"]

    with pytest.raises(ArtifactError, match="checksum"):
        read_artifact(tmp_path, "source.ndjson", "0" * 64)
    with pytest.raises(ArtifactError, match="outside"):
        read_artifact(tmp_path, "../elsewhere.ndjson", None)

    artifact.write_text(
        "\n".join(json.dumps({"external_id": "one", "name": name}) for name in ("A", "B")),
        encoding="utf-8",
    )
    with pytest.raises(ArtifactError, match="duplicate"):
        read_artifact(tmp_path, "source.ndjson", None)


def test_read_artifact_accepts_gzip_and_hashes_stored_bytes(tmp_path) -> None:
    artifact = tmp_path / "source.ndjson.gz"
    body = gzip.compress(b'{"external_id":"one","name":"One"}\n', mtime=0)
    artifact.write_bytes(body)
    rows = read_artifact(tmp_path, artifact.name, hashlib.sha256(body).hexdigest())
    assert rows["one"]["name"] == "One"
