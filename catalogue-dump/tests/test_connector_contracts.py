from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from mb_ceramics_catalogue.connectors import (
    Availability,
    BrowserBackendName,
    BrowserRequirement,
    CollectionRequest,
    CommerceOffer,
    CommerceProductSnapshot,
    CommerceVariant,
    ConnectorCapabilities,
    Diagnostic,
    DiagnosticCode,
    DiagnosticSeverity,
    EntityPage,
    Evidence,
    Money,
    RefreshMode,
    SnapshotField,
    StockQuantityKind,
    StockState,
)

NOW = datetime(2026, 8, 15, 10, 30, tzinfo=UTC)


def evidence() -> Evidence:
    return Evidence(method="api", source_url="https://shop.test/products/1.json", observed_at=NOW)


def snapshot() -> CommerceProductSnapshot:
    offer = CommerceOffer(
        price=Money(amount=Decimal("12.30"), currency="EUR"),
        observed_at=NOW,
        evidence=(evidence(),),
        vat_status="inclusive",
    )
    stock = StockState(
        availability=Availability.IN_STOCK,
        quantity=7,
        quantity_kind=StockQuantityKind.EXACT,
        observed_at=NOW,
        evidence=(evidence(),),
    )
    return CommerceProductSnapshot(
        connector="shopify",
        source_id="shop",
        external_id="product-1",
        canonical_url="https://shop.test/products/one",
        title="Stoneware clay",
        observed_at=NOW,
        variants=(CommerceVariant(external_id="variant-1", offers=(offer,), stock=stock),),
    )


def test_snapshot_round_trips_without_float_money() -> None:
    original = snapshot()
    restored = CommerceProductSnapshot.model_validate_json(original.model_dump_json())
    assert restored == original
    assert restored.variants[0].offers[0].price.amount == Decimal("12.30")
    assert restored.contract_version == "commerce.product_snapshot.v1"


def test_money_rejects_python_float_and_bad_currency() -> None:
    with pytest.raises(ValidationError):
        Money.model_validate({"amount": 12.3, "currency": "EUR"})
    with pytest.raises(ValidationError):
        Money(amount=Decimal("12.30"), currency="eur")
    with pytest.raises(ValidationError):
        Money(amount=Decimal("-0.01"), currency="EUR")


def test_observation_times_must_include_a_timezone() -> None:
    with pytest.raises(ValidationError):
        Evidence(method="api", source_url="https://shop.test/product", observed_at=NOW.replace(tzinfo=None))


def test_contracts_reject_unknown_fields_and_are_frozen() -> None:
    with pytest.raises(ValidationError):
        Evidence.model_validate(
            {
                "method": "api",
                "source_url": "https://shop.test/product",
                "observed_at": NOW,
                "unreviewed": "value",
            }
        )
    with pytest.raises(ValidationError):
        snapshot().title = "changed"


def test_page_states_distinguish_pagination_failure_and_success() -> None:
    ordinary = EntityPage[CommerceProductSnapshot](
        page_id="page-1",
        sequence=0,
        items=(snapshot(),),
        resume_after={"cursor": "next"},
        terminal=False,
        discovered=1,
    )
    assert ordinary.enumeration_intact

    complete = EntityPage[CommerceProductSnapshot](
        page_id="page-2", sequence=1, items=(), terminal=True, enumeration_intact=True, discovered=1
    )
    assert complete.resume_after is None

    failure = EntityPage[CommerceProductSnapshot](
        page_id="page-failed",
        sequence=1,
        items=(),
        terminal=True,
        enumeration_intact=False,
        discovered=1,
        diagnostics=(
            Diagnostic(
                code=DiagnosticCode.ENUMERATION_INCOMPLETE,
                severity=DiagnosticSeverity.ERROR,
                message="pagination stopped before the terminal cursor",
                retryable=True,
                affects_completeness=True,
            ),
        ),
    )
    assert failure.diagnostics[0].affects_completeness


@pytest.mark.parametrize(
    "values",
    [
        {"terminal": True, "enumeration_intact": True, "resume_after": "next"},
        {"terminal": False, "enumeration_intact": False, "resume_after": "next"},
    ],
)
def test_invalid_page_state_is_rejected(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        EntityPage[CommerceProductSnapshot].model_validate(
            {"page_id": "bad", "sequence": 0, "items": [], "discovered": 0, **values}
        )


def test_page_partition_and_sequence_are_explicit() -> None:
    page = EntityPage[CommerceProductSnapshot](
        page_id="page-1", sequence=0, items=(), terminal=True, discovered=0
    )
    assert page.partition_key == "main"
    with pytest.raises(ValidationError):
        EntityPage[CommerceProductSnapshot](
            page_id="page-before-start", sequence=-1, items=(), terminal=True, discovered=0
        )


def test_capabilities_validate_browser_and_incremental_declarations() -> None:
    capabilities = ConnectorCapabilities(
        snapshot_fields=frozenset({SnapshotField.IDENTITY, SnapshotField.OFFERS}),
        refresh_modes=frozenset({RefreshMode.FULL, RefreshMode.INCREMENTAL}),
        supports_incremental_cursor=True,
        browser=BrowserRequirement.OPTIONAL,
        browser_backends=frozenset({BrowserBackendName.CAMOUFOX}),
    )
    assert capabilities.supports(frozenset({SnapshotField.OFFERS}), RefreshMode.INCREMENTAL)
    assert not capabilities.supports(frozenset({SnapshotField.STOCK}), RefreshMode.FULL)
    assert capabilities.named_capabilities() >= {
        "incremental_cursor",
        "browser:camoufox",
    }

    with pytest.raises(ValidationError, match="must declare"):
        ConnectorCapabilities(
            snapshot_fields=frozenset({SnapshotField.IDENTITY}),
            refresh_modes=frozenset({RefreshMode.FULL}),
            browser=BrowserRequirement.REQUIRED,
        )


def test_collection_request_callbacks_are_runtime_only() -> None:
    request = CollectionRequest(
        source_id="shop",
        base_url="https://shop.test",
        refresh_mode=RefreshMode.FULL,
        requested_fields=frozenset({SnapshotField.IDENTITY}),
        cancellation_check=lambda: True,
    )
    assert request.cancelled()
    assert "cancellation_check" not in request.model_dump()
