from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from mb_ceramics_catalogue import scrapers
from mb_ceramics_catalogue.connectors import (
    CollectionRequest,
    ConnectorCheckpoint,
    DiagnosticCode,
    RefreshMode,
    SnapshotField,
    StockQuantityKind,
    WooCommerceConnector,
    WooCommerceOptions,
)
from mb_ceramics_catalogue.datasets import CeramicsCatalogueProjector, ProjectionContext
from mb_ceramics_catalogue.pipeline.budget import RequestBudget, RequestCost, RequestPriority
from mb_ceramics_catalogue.scrapers.woocommerce import WooCommerceScraper

NOW = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)


class FakeFetcher:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def json(self, url: str, *, params=None, headers=None):
        self.calls.append((url, params))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)


def request(
    *, limit: int | None = None, categories: tuple[str, ...] = (),
    fields: frozenset[SnapshotField] | None = None,
) -> CollectionRequest:
    return CollectionRequest(
        source_id="woo-shop",
        base_url="https://shop.test/catalogue",
        refresh_mode=RefreshMode.FULL,
        requested_fields=fields or frozenset(SnapshotField),
        result_limit=limit,
        categories=categories,
    )


def prices(price: str = "1250", regular: str = "1500") -> dict[str, Any]:
    return {
        "price": price,
        "regular_price": regular,
        "sale_price": price,
        "currency_code": "EUR",
        "currency_minor_unit": 2,
    }


def product(identifier: int = 10, *, variable: bool = False) -> dict[str, Any]:
    return {
        "id": identifier,
        "type": "variable" if variable else "simple",
        "name": "Transparent &amp; Gloss Glaze",
        "permalink": f"https://shop.test/product/glaze-{identifier}",
        "description": '<p>A gloss glaze.</p><a href="/docs/sds.pdf">SDS</a>',
        "short_description": "Gloss",
        "sku": f"GL-{identifier}",
        "prices": prices(),
        "price_html": "<span>€12.50</span>",
        "is_in_stock": True,
        "is_on_backorder": False,
        "sold_individually": False,
        "add_to_cart": {"maximum": 7},
        "brands": [{"name": "Test Ceramics"}],
        "categories": [{"id": 3, "name": "Glazes", "slug": "glazes"}],
        "images": [
            {"id": 5, "src": "https://cdn.test/glaze.jpg", "alt": "Blue glaze"}
        ],
        "attributes": [
            {"name": "Firing range", "terms": [{"name": "Cone 6"}]},
            {
                "name": "Food safe",
                "terms": [{"name": "https://cdn.test/icons/food-safe.png"}],
            },
        ],
        "variations": [identifier * 10] if variable else [],
    }


def variation(parent: int = 10) -> dict[str, Any]:
    return {
        "id": parent * 10,
        "parent": parent,
        "type": "variation",
        "permalink": f"https://shop.test/product/glaze-{parent}?attribute_size=500ml",
        "variation": "500 ml / Blue",
        "sku": f"GL-{parent}-500",
        "prices": prices(),
        "is_in_stock": True,
        "stock_availability": {"quantity": 4},
        "attributes": [
            {"name": "Size", "value": "500 ml"},
            {"name": "Colour", "value": "Blue"},
        ],
        "images": [{"src": "https://cdn.test/variant.jpg"}],
    }


async def collect(connector: WooCommerceConnector, intent: CollectionRequest, checkpoint=None):
    return [page async for page in connector.collect(intent, checkpoint)]


@pytest.mark.asyncio
async def test_required_variation_work_uses_dataset_priority_and_exhausts_resumably() -> None:
    fetcher = FakeFetcher([[product(variable=True)]])
    budget = RequestBudget(RequestCost(http_requests=1))
    connector = WooCommerceConnector(fetcher, budget=budget)

    [page] = await collect(connector, request())

    assert page.terminal and not page.enumeration_intact
    assert page.resume_after == {"partition": "main", "page": 1}
    assert page.diagnostics[0].code == DiagnosticCode.REQUEST_BUDGET_EXHAUSTED
    assert budget.used_by_priority == {
        RequestPriority.DISCOVERY: RequestCost(http_requests=1)
    }
    assert len(fetcher.calls) == 1


@pytest.mark.asyncio
async def test_simple_product_preserves_parity_critical_neutral_fields() -> None:
    fetcher = FakeFetcher([[product()]])
    connector = WooCommerceConnector(
        fetcher,
        WooCommerceOptions(
            vat_status="inclusive", stock_from_add_to_cart_maximum=True
        ),
        clock=lambda: NOW,
    )

    pages = await collect(connector, request())

    assert len(pages) == 1 and pages[0].terminal and pages[0].enumeration_intact
    snapshot = pages[0].items[0]
    assert snapshot.title == "Transparent & Gloss Glaze"
    assert snapshot.vendor == "Test Ceramics"
    assert [category.name for category in snapshot.categories] == ["Glazes"]
    assert snapshot.images[0].url == "https://cdn.test/glaze.jpg"
    assert snapshot.documents[0].url == "https://shop.test/docs/sds.pdf"
    assert snapshot.published_attributes["Firing range"] == "Cone 6"
    assert snapshot.published_attributes["claims"][0]["type"] == "food_contact_suitability"
    variant = snapshot.variants[0]
    assert variant.sku == "GL-10"
    assert variant.offers[0].price.amount.as_tuple().exponent == -2
    assert variant.offers[0].price.amount == 12.50
    assert variant.offers[0].role == "sale"
    assert variant.offers[1].price.amount == 15
    assert variant.stock.quantity == 7
    assert variant.stock.quantity_kind == StockQuantityKind.EXACT
    raw = snapshot.platform_extensions["raw"]
    assert isinstance(raw, dict) and raw["id"] == 10


@pytest.mark.asyncio
async def test_bulk_variation_join_preserves_options_image_offer_and_stock() -> None:
    fetcher = FakeFetcher([[product(variable=True)], [variation()]])
    connector = WooCommerceConnector(fetcher, clock=lambda: NOW)

    pages = await collect(connector, request())

    variant_snapshot = pages[0].items[0].variants[0]
    assert variant_snapshot.external_id == "100"
    assert variant_snapshot.title == "500 ml / Blue"
    assert variant_snapshot.options == {"Size": "500 ml", "Colour": "Blue"}
    assert variant_snapshot.image.url == "https://cdn.test/variant.jpg"
    assert variant_snapshot.stock.quantity == 4
    assert fetcher.calls[1][1] == {"per_page": 100, "page": 1, "type": "variation"}


@pytest.mark.asyncio
async def test_missing_variation_keeps_legacy_parent_offer_with_warning() -> None:
    fetcher = FakeFetcher([[product(variable=True)], []])
    connector = WooCommerceConnector(fetcher, clock=lambda: NOW)

    pages = await collect(connector, request())

    snapshot = pages[0].items[0]
    assert snapshot.variants[0].external_id == "10"
    assert snapshot.variants[0].offers[0].price.amount == 12.50
    assert "returned no variations" in pages[0].diagnostics[0].message


@pytest.mark.asyncio
async def test_checkpoint_resumes_at_exact_product_page() -> None:
    fetcher = FakeFetcher([[]])
    connector = WooCommerceConnector(fetcher, clock=lambda: NOW)
    checkpoint = ConnectorCheckpoint(
        connector="woocommerce",
        connector_version="1",
        source_id="woo-shop",
        lineage="lineage-1",
        resume_after={"partition": "main", "page": 4},
    )

    pages = await collect(connector, request(), checkpoint)

    assert pages[0].terminal and pages[0].items == ()
    assert fetcher.calls[0][1] is not None
    assert fetcher.calls[0][1]["page"] == 4


@pytest.mark.asyncio
async def test_checkpoint_partition_uses_declared_order_not_lexical_order() -> None:
    fetcher = FakeFetcher(
        [
            [{"id": 1, "slug": "zeta"}, {"id": 2, "slug": "alpha"}],
            [],
        ]
    )
    connector = WooCommerceConnector(fetcher, clock=lambda: NOW)
    checkpoint = ConnectorCheckpoint(
        connector="woocommerce",
        connector_version="1",
        source_id="woo-shop",
        lineage="lineage-ordered",
        resume_after={"partition": "alpha", "page": 3},
    )

    await collect(
        connector,
        request(categories=("zeta", "alpha")),
        checkpoint,
    )

    assert fetcher.calls[1][1] == {"per_page": 100, "page": 3, "category": 2}


@pytest.mark.asyncio
async def test_store_api_400_after_first_page_is_a_clean_end() -> None:
    full = [product(index) for index in range(1, 3)]
    response = httpx.Response(400, request=httpx.Request("GET", "https://shop.test"))
    fetcher = FakeFetcher([full, httpx.HTTPStatusError("past end", request=response.request, response=response)])
    connector = WooCommerceConnector(
        fetcher, WooCommerceOptions(page_size=2), clock=lambda: NOW
    )

    pages = await collect(connector, request())

    assert len(pages) == 1
    assert pages[0].terminal and pages[0].enumeration_intact
    assert len(pages[0].items) == 2
    assert [call[1]["page"] for call in fetcher.calls if call[1] is not None] == [1, 2]


@pytest.mark.asyncio
async def test_result_limit_is_incomplete_and_resumes_within_store_api_page() -> None:
    fetcher = FakeFetcher([[product(1), product(2)]])
    connector = WooCommerceConnector(fetcher, clock=lambda: NOW)

    [page] = await collect(connector, request(limit=1))

    assert page.terminal and not page.enumeration_intact
    assert len(page.items) == 1
    assert page.resume_after == {"partition": "main", "page": 1, "offset": 1}
    assert page.diagnostics[-1].code == DiagnosticCode.RESULT_LIMIT_REACHED

    checkpoint = ConnectorCheckpoint(
        connector="woocommerce",
        connector_version="1",
        source_id="woo-shop",
        lineage="limited",
        resume_after=page.resume_after,
    )
    resumed = WooCommerceConnector(
        FakeFetcher([[product(1), product(2)]]), clock=lambda: NOW
    )
    [resumed_page] = await collect(resumed, request(), checkpoint)
    assert [item.external_id for item in resumed_page.items] == ["2"]


@pytest.mark.asyncio
async def test_product_failure_is_incomplete_and_restarts_first_uncommitted_buffer() -> None:
    fetcher = FakeFetcher([[product()], httpx.ReadTimeout("slow second page")])
    connector = WooCommerceConnector(
        fetcher, WooCommerceOptions(page_size=1), clock=lambda: NOW
    )

    pages = await collect(connector, request())

    assert len(pages) == 1
    assert pages[0].terminal and not pages[0].enumeration_intact
    assert pages[0].items == ()
    assert pages[0].resume_after == {"partition": "main", "page": 1}
    assert pages[0].diagnostics[0].code == DiagnosticCode.ENUMERATION_INCOMPLETE


@pytest.mark.asyncio
async def test_product_page_limit_bounds_requests_and_marks_incomplete() -> None:
    fetcher = FakeFetcher([[product(1)], [product(2)]])
    connector = WooCommerceConnector(
        fetcher, WooCommerceOptions(page_size=1, page_limit=2), clock=lambda: NOW
    )

    pages = await collect(connector, request())

    assert len(fetcher.calls) == 2
    assert len(pages) == 1 and not pages[0].enumeration_intact
    assert "page limit 2" in pages[0].diagnostics[0].message


@pytest.mark.asyncio
async def test_variation_page_limit_is_bounded_and_does_not_commit_partial_shape() -> None:
    fetcher = FakeFetcher([[product(variable=True)], [], [variation()]])
    connector = WooCommerceConnector(
        fetcher,
        WooCommerceOptions(page_size=1, variation_page_limit=1),
        clock=lambda: NOW,
    )

    pages = await collect(connector, request())

    assert len(fetcher.calls) == 3
    assert len(pages) == 1 and pages[0].items == ()
    assert not pages[0].enumeration_intact
    assert pages[0].resume_after == {"partition": "main", "page": 1}


@pytest.mark.asyncio
async def test_category_resolution_filters_and_carries_partition_checkpoint() -> None:
    fetcher = FakeFetcher(
        [
            [{"id": 3, "slug": "glazes"}],
            [product()],
        ]
    )
    connector = WooCommerceConnector(fetcher, clock=lambda: NOW)

    pages = await collect(connector, request(categories=("glazes", "missing")))

    assert fetcher.calls[1][1] is not None
    assert fetcher.calls[1][1]["category"] == 3
    assert pages[0].partition_key == "glazes"
    assert [category.name for category in pages[0].items[0].categories] == ["glazes", "Glazes"]
    assert "missing" in pages[0].diagnostics[0].message


@pytest.mark.asyncio
async def test_category_failure_checkpoint_restarts_requested_partition() -> None:
    connector = WooCommerceConnector(
        FakeFetcher([httpx.ReadTimeout("categories slow")]), clock=lambda: NOW
    )

    pages = await collect(connector, request(categories=("glazes",)))

    assert not pages[0].enumeration_intact
    assert pages[0].resume_after == {"partition": "glazes", "page": 1}


@pytest.mark.asyncio
async def test_identity_only_omits_zero_price_offers() -> None:
    item = product()
    item["prices"] = prices("0", "0")
    connector = WooCommerceConnector(
        FakeFetcher([[item]]), WooCommerceOptions(identity_only=True), clock=lambda: NOW
    )
    identity_fields = frozenset(
        field for field in SnapshotField if field != SnapshotField.OFFERS
    )

    pages = await collect(connector, request(fields=identity_fields))

    assert pages[0].items[0].variants[0].offers == ()
    with pytest.raises(ValueError, match="identity-only"):
        await collect(connector, request())


@pytest.mark.asyncio
async def test_neutral_projection_matches_legacy_commercial_and_descriptive_fields() -> None:
    item = product()
    legacy = WooCommerceScraper(
        "woo-shop",
        {
            "url": "https://shop.test",
            "scope": "all",
            "vat_status": "inclusive",
            "stock_from_add_to_cart_maximum": True,
        },
        FakeFetcher([[item]]),  # type: ignore[arg-type]
    )
    legacy_result = await legacy.scrape()

    connector = WooCommerceConnector(
        FakeFetcher([[item]]),
        WooCommerceOptions(vat_status="inclusive", stock_from_add_to_cart_maximum=True),
        clock=lambda: NOW,
    )
    pages = await collect(connector, request())
    projector = CeramicsCatalogueProjector()
    projected = tuple(
        projector.project(
            pages[0].items[0],
            ProjectionContext(
                collection_id="parity",
                source_id="woo-shop",
                dataset=projector.name,
                dataset_version=projector.version,
                projector_version=projector.projector_version,
                configuration={
                    "scope": "all",
                    "apply_scope": False,
                },
            ),
        )
    )

    assert len(projected) == len(legacy_result.records) == 1
    new = projected[0].as_legacy_dict()
    old = legacy_result.records[0]
    for field in (
        "product_name",
        "brand",
        "supplier_reference",
        "description",
        "category_path",
        "image_url",
        "all_image_urls",
        "price",
        "currency",
        "list_price",
        "vat_status",
        "availability",
        "stock_quantity",
        "documents",
        "technical_attributes",
        "raw",
    ):
        assert new[field] == old[field], field


@pytest.mark.asyncio
async def test_canary_adapter_is_explicit_and_legacy_key_is_unchanged() -> None:
    item = product()
    configuration = {
        "url": "https://shop.test",
        "scope": "all",
        "vat_status": "inclusive",
        "stock_from_add_to_cart_maximum": True,
    }
    legacy = scrapers.build("woocommerce", "woo-shop", configuration, FakeFetcher([[item]]))
    canary = scrapers.build(
        "woocommerce_connector", "woo-shop", configuration, FakeFetcher([[item]])
    )

    legacy_result = await legacy.scrape()
    canary_result = await canary.scrape()

    assert scrapers.load("woocommerce").__name__ == "WooCommerceScraper"
    assert scrapers.load("woocommerce_connector").__name__ == "WooCommerceConnectorScraper"
    assert len(canary_result.records) == len(legacy_result.records) == 1
    for field in (
        "product_name",
        "brand",
        "supplier_reference",
        "price",
        "list_price",
        "vat_status",
        "availability",
        "stock_quantity",
        "technical_attributes",
        "raw",
    ):
        assert canary_result.records[0][field] == legacy_result.records[0][field], field


@pytest.mark.asyncio
async def test_identity_only_canary_emits_identity_record_without_offer() -> None:
    item = product()
    item["prices"] = prices("0", "0")
    scraper = scrapers.build(
        "woocommerce_connector",
        "mayco",
        {
            "url": "https://shop.test",
            "scope": "all",
            "identity_only": True,
            "brand": "Mayco",
            "is_manufacturer": True,
        },
        FakeFetcher([[item]]),
    )

    result = await scraper.scrape()

    assert len(result.records) == 1
    assert result.records[0]["format"] == "ceramics.catalogue_identity.v2"
    assert result.records[0]["price"] is None
