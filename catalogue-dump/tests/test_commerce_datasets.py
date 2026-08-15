from datetime import UTC, datetime
from decimal import Decimal

from mb_ceramics_catalogue.connectors import (
    Availability,
    CommerceOffer,
    CommerceProductSnapshot,
    CommerceVariant,
    DocumentRef,
    Evidence,
    Money,
    StockQuantityKind,
    StockState,
)
from mb_ceramics_catalogue.datasets import ProjectionContext, built_in_registry

NOW = datetime(2026, 8, 15, 12, tzinfo=UTC)


def snapshot():
    evidence = Evidence(method="api", source_url="https://shop.test/p.json", observed_at=NOW)
    return CommerceProductSnapshot(
        connector="shopify",
        source_id="shop",
        external_id="p1",
        canonical_url="https://shop.test/p",
        title="Clay",
        observed_at=NOW,
        documents=(
            DocumentRef(
                url="https://shop.test/sds.pdf",
                observed_at=NOW,
                evidence=(evidence,),
            ),
        ),
        variants=(
            CommerceVariant(
                external_id="v1",
                offers=(
                    CommerceOffer(
                        price=Money(amount=Decimal("12.50"), currency="EUR"),
                        observed_at=NOW,
                        evidence=(evidence,),
                        availability=Availability.IN_STOCK,
                        availability_evidence=(evidence,),
                    ),
                ),
                stock=StockState(
                    availability=Availability.IN_STOCK,
                    quantity=3,
                    quantity_kind=StockQuantityKind.EXACT,
                    observed_at=NOW,
                    evidence=(evidence,),
                ),
            ),
        ),
    )


def project(name):
    registry = built_in_registry()
    definition = registry.get(name)
    context = ProjectionContext(
        collection_id="lineage",
        source_id="shop",
        dataset=name,
        dataset_version=definition.version,
        projector_version=definition.projector_version,
    )
    return list(registry.project_validated(name, snapshot(), context))


def test_built_in_registry_is_complete():
    assert set(built_in_registry().names()) == {
        "ceramics.catalogue_item.v2",
        "ceramics.catalogue_identity.v2",
        "commerce.price_observation.v1",
        "commerce.stock_observation.v1",
        "commerce.document.v1",
    }


def test_price_observation_is_deterministic_and_lossless():
    [first] = project("commerce.price_observation.v1")
    [again] = project("commerce.price_observation.v1")
    assert first == again
    assert first.amount == Decimal("12.50")
    assert first.currency == "EUR"


def test_stock_quantity_kind_is_preserved():
    [row] = project("commerce.stock_observation.v1")
    assert row.quantity == 3
    assert row.quantity_kind == StockQuantityKind.EXACT


def test_document_keeps_its_own_timestamp_and_evidence():
    [row] = project("commerce.document.v1")
    assert row.document_url.endswith("sds.pdf")
    assert row.observed_at == NOW
    assert row.evidence[0].source_url.endswith("p.json")
