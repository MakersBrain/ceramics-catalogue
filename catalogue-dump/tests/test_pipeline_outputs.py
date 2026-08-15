from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from mb_ceramics_catalogue.pipeline.outputs import BatchIdentity, LocalArtifactStore, StoredBatch


def identity(page: str = "page-1", sequence: int | None = None) -> BatchIdentity:
    return BatchIdentity(
        job_id="job-1",
        checkpoint_lineage="lineage-1",
        partition_key="default",
        page_id=page,
        page_sequence=sequence if sequence is not None else int(page.rsplit("-", 1)[-1]),
        dataset="ceramics.catalogue_item.v2",
        contract_version="2",
        projector_version="1",
    )


def read(location: str) -> list[dict[str, object]]:
    with gzip.open(location, "rt", encoding="utf-8") as source:
        return [json.loads(line) for line in source]


def test_stage_batch_is_deterministic_and_idempotent(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    first = store.stage_batch(identity(), [{"b": 2, "a": 1}])
    second = store.stage_batch(identity(), [{"a": 1, "b": 2}])

    assert first == second
    assert first.records == 1
    assert read(first.location) == [{"a": 1, "b": 2}]


def test_stage_batch_rejects_nondeterministic_projection(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    store.stage_batch(identity(), [{"value": 1}])

    with pytest.raises(ValueError, match="staged batch changed"):
        store.stage_batch(identity(), [{"value": 2}])


def test_publish_streams_verified_batches_in_order(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    first = store.stage_batch(identity("page-1"), ({"n": value} for value in range(2)))
    second = store.stage_batch(identity("page-2"), [{"n": 2}])

    published = store.publish_dataset("job-1", "ceramics.catalogue_item.v2", "2", [first, second])

    assert published.records == 3
    assert read(published.location) == [{"n": 0}, {"n": 1}, {"n": 2}]
    assert store.publish_dataset(
        "job-1", "ceramics.catalogue_item.v2", "2", [first, second]
    ) == published


def test_publish_rejects_tampered_or_duplicate_batches(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    batch = store.stage_batch(identity(), [{"value": 1}])
    tampered = StoredBatch(
        batch.location, "0" * 64, batch.size, batch.records, batch.identity
    )

    with pytest.raises(ValueError, match="checksum mismatch"):
        store.publish_dataset("job-1", "dataset", "1", [tampered])
    with pytest.raises(ValueError, match="duplicate page batch"):
        store.publish_dataset("job-1", "dataset", "1", [batch, batch])


def test_components_and_locations_cannot_escape_store(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    unsafe = BatchIdentity("../job", "lineage", "default", "page", 0, "dataset", "1", "1")

    with pytest.raises(ValueError, match="unsafe artifact component"):
        store.stage_batch(unsafe, [])

    opaque = identity("https://shop.test/products.json?page=2", 2)
    staged = store.stage_batch(opaque, [{"safe": True}])
    assert Path(staged.location).is_relative_to(tmp_path)
