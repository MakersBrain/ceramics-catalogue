from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ConfigDict

from mb_ceramics_catalogue.connectors import CommerceProductSnapshot, SnapshotField
from mb_ceramics_catalogue.datasets import DATASET_NAMES, DatasetRegistry, ProjectionContext


class TitleRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    external_id: str
    title: str


class TitleDataset:
    name = "test.title.v1"
    version = "1"
    projector_version = "fixture-1"

    @property
    def record_model(self) -> type[TitleRecord]:
        return TitleRecord

    required_snapshot_fields = frozenset({SnapshotField.IDENTITY})
    required_capabilities: frozenset[str] = frozenset()

    def project(self, entity: CommerceProductSnapshot, context: ProjectionContext) -> Iterable[TitleRecord]:
        yield TitleRecord(external_id=entity.external_id, title=entity.title)


def entity() -> CommerceProductSnapshot:
    return CommerceProductSnapshot(
        connector="fixture",
        source_id="source",
        external_id="one",
        canonical_url="https://example.test/one",
        title="Clay",
        observed_at=datetime(2026, 8, 15, tzinfo=UTC),
    )


def context(**updates: str) -> ProjectionContext:
    return ProjectionContext(
        collection_id="collection",
        source_id=updates.get("source_id", "source"),
        dataset=updates.get("dataset", TitleDataset.name),
        dataset_version=updates.get("dataset_version", TitleDataset.version),
        projector_version=updates.get("projector_version", TitleDataset.projector_version),
    )


def test_registry_resolves_and_validates_projection_lazily() -> None:
    registry = DatasetRegistry([TitleDataset()])
    records = list(registry.project_validated(TitleDataset.name, entity(), context()))
    assert records == [TitleRecord(external_id="one", title="Clay")]
    assert registry.names() == (TitleDataset.name,)
    assert registry.collection_requirements([TitleDataset.name]) == (
        frozenset({SnapshotField.IDENTITY}),
        frozenset(),
    )


def test_duplicate_and_unknown_datasets_fail_with_names() -> None:
    registry = DatasetRegistry([TitleDataset()])
    with pytest.raises(ValueError, match="already registered"):
        registry.register(TitleDataset())
    with pytest.raises(KeyError) as caught:
        registry.get("missing")
    assert "test.title.v1" in str(caught.value)


def test_projection_context_must_match_contract_and_projector_versions() -> None:
    registry = DatasetRegistry([TitleDataset()])
    with pytest.raises(ValueError, match="dataset contract"):
        list(registry.project_validated(TitleDataset.name, entity(), context(dataset_version="2")))
    with pytest.raises(ValueError, match="projector version"):
        list(registry.project_validated(TitleDataset.name, entity(), context(projector_version="new")))
    with pytest.raises(ValueError, match="entity source"):
        list(registry.project_validated(TitleDataset.name, entity(), context(source_id="another")))


def test_planned_dataset_names_are_frozen() -> None:
    assert {
        "ceramics.catalogue_item.v2",
        "ceramics.catalogue_identity.v2",
        "commerce.price_observation.v1",
        "commerce.stock_observation.v1",
        "commerce.document.v1",
    } == DATASET_NAMES
