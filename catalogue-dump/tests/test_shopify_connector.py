from datetime import UTC, datetime

import httpx
import pytest

from mb_ceramics_catalogue.connectors.base import (
    CollectionRequest,
    ConnectorCheckpoint,
    DiagnosticCode,
    RefreshMode,
    SnapshotField,
)
from mb_ceramics_catalogue.connectors.commerce import StockQuantityKind
from mb_ceramics_catalogue.connectors.shopify import (
    ShopifyConnector,
    ShopifyOptions,
)
from mb_ceramics_catalogue.pipeline.budget import RequestBudget, RequestCost

NOW = datetime(2026, 8, 15, tzinfo=UTC)


class FakeFetcher:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    async def json(self, url, *, params=None, headers=None):
        self.calls.append((url, params))
        if url.endswith("meta.json"):
            return {"currency": "EUR"}
        value = self.pages.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    async def text(self, url, *, headers=None):
        return await self.json(url, headers=headers)

    async def rotate_client(self):
        self.calls.append(("rotate", None))


def request(*, limit=None):
    return CollectionRequest(
        source_id="shop",
        base_url="https://shop.test/path",
        refresh_mode=RefreshMode.FULL,
        requested_fields=frozenset({SnapshotField.IDENTITY, SnapshotField.OFFERS}),
        result_limit=limit,
    )


def product(identifier="10"):
    return {
        "id": identifier,
        "handle": f"clay-{identifier}",
        "title": "Stoneware Clay",
        "vendor": "Clay Co",
        "product_type": "Clay",
        "updated_at": "2026-08-14T12:00:00Z",
        "images": [{"id": 8, "src": "https://cdn.test/clay.jpg"}],
        "variants": [
            {
                "id": 20,
                "title": "10 kg",
                "price": "12.50",
                "available": True,
                "inventory_management": "shopify",
                "inventory_policy": "deny",
                "inventory_quantity": 7,
                "sku": "CLAY-10",
            }
        ],
    }


@pytest.mark.asyncio
async def test_shopify_emits_neutral_terminal_page():
    fetcher = FakeFetcher([{"products": [product()]}])
    connector = ShopifyConnector(fetcher, clock=lambda: NOW)

    pages = [page async for page in connector.collect(request())]

    assert len(pages) == 1
    assert pages[0].terminal and pages[0].enumeration_intact
    assert pages[0].sequence == 0
    snapshot = pages[0].items[0]
    assert snapshot.external_id == "10"
    assert snapshot.variants[0].offers[0].price.amount.as_tuple().exponent == -2
    assert snapshot.variants[0].stock.quantity == 7
    assert snapshot.variants[0].stock.quantity_kind == StockQuantityKind.EXACT
    assert fetcher.calls[1][0] == "https://shop.test/products.json"


@pytest.mark.asyncio
async def test_shopify_resumes_at_page_boundary():
    fetcher = FakeFetcher([{"products": []}])
    connector = ShopifyConnector(fetcher, ShopifyOptions(currency="EUR"), clock=lambda: NOW)
    checkpoint = ConnectorCheckpoint(
        connector="shopify",
        connector_version="1",
        source_id="shop",
        lineage="lineage-1",
        resume_after={"partition": "main", "page": 4},
    )

    pages = [page async for page in connector.collect(request(), checkpoint)]

    assert pages[0].page_id == "main:4"
    assert pages[0].sequence == 3
    assert fetcher.calls[0][1]["page"] == 4


@pytest.mark.asyncio
async def test_shopify_resume_uses_declared_partition_order_not_lexical_order():
    fetcher = FakeFetcher([{"products": []}])
    connector = ShopifyConnector(fetcher, ShopifyOptions(currency="EUR"), clock=lambda: NOW)
    selected = request().model_copy(update={"collections": ("z-last-lexically", "a-second")})
    checkpoint = ConnectorCheckpoint(
        connector="shopify",
        connector_version="1",
        source_id="shop",
        lineage="lineage-1",
        resume_after={"partition": "a-second", "page": 2},
    )

    pages = [page async for page in connector.collect(selected, checkpoint)]

    assert pages[0].partition_key == "a-second"
    assert fetcher.calls[0][0].endswith("/collections/a-second/products.json")
    assert fetcher.calls[0][1]["page"] == 2


@pytest.mark.asyncio
async def test_shopify_failure_is_incomplete_and_resumable():
    fetcher = FakeFetcher([httpx.ReadTimeout("slow")])
    connector = ShopifyConnector(fetcher, ShopifyOptions(currency="EUR"), clock=lambda: NOW)

    pages = [page async for page in connector.collect(request())]

    assert pages[0].terminal and not pages[0].enumeration_intact
    assert pages[0].resume_after == {"partition": "main", "page": 1}
    assert pages[0].diagnostics[0].code == DiagnosticCode.ENUMERATION_INCOMPLETE


@pytest.mark.asyncio
async def test_shopify_limit_does_not_fetch_extra_page():
    fetcher = FakeFetcher([{"products": [product("1"), product("2")]}])
    connector = ShopifyConnector(fetcher, ShopifyOptions(currency="EUR"), clock=lambda: NOW)

    pages = [page async for page in connector.collect(request(limit=1))]

    assert len(pages) == 1
    assert pages[0].terminal
    assert not pages[0].enumeration_intact
    assert pages[0].resume_after == {"partition": "main", "page": 1, "offset": 1}
    assert pages[0].diagnostics[-1].code == DiagnosticCode.RESULT_LIMIT_REACHED
    assert len(pages[0].items) == 1
    assert len(fetcher.calls) == 1

    checkpoint = ConnectorCheckpoint(
        connector="shopify",
        connector_version="1",
        source_id="shop",
        lineage="limited",
        resume_after=pages[0].resume_after,
    )
    resumed = ShopifyConnector(
        FakeFetcher([{"products": [product("1"), product("2")]}]),
        ShopifyOptions(currency="EUR"),
        clock=lambda: NOW,
    )
    [resumed_page] = [page async for page in resumed.collect(request(), checkpoint)]
    assert [item.external_id for item in resumed_page.items] == ["2"]


@pytest.mark.asyncio
async def test_inventory_budget_preserves_the_next_discovery_page() -> None:
    products = [product(str(index)) for index in range(1, 4)]
    for item in products:
        variant = item["variants"][0]
        variant.pop("inventory_quantity")
        variant.pop("inventory_management")
        variant.pop("inventory_policy")
    detail = {
        "variants": [
            {
                "id": 20,
                "inventory_quantity": 9,
                "inventory_management": "shopify",
                "inventory_policy": "deny",
            }
        ]
    }
    fetcher = FakeFetcher([{"products": products}, detail, {"products": []}])
    budget = RequestBudget(RequestCost(http_requests=3, proxy_bytes=3_000_000))
    connector = ShopifyConnector(
        fetcher,
        ShopifyOptions(
            currency="EUR",
            page_size=3,
            inventory_method="product_json",
        ),
        clock=lambda: NOW,
        budget=budget,
    )

    pages = [page async for page in connector.collect(request())]

    assert len(fetcher.calls) == 3  # two feed pages and one optional detail request
    assert len(pages) == 2 and pages[-1].terminal
    first_stock = pages[0].items[0].variants[0].stock
    second_stock = pages[0].items[1].variants[0].stock
    assert first_stock is not None and first_stock.quantity == 9
    assert second_stock is not None and second_stock.quantity is None
    assert pages[0].diagnostics[0].code == DiagnosticCode.OPTIONAL_ENRICHMENT_SKIPPED


@pytest.mark.asyncio
async def test_inventory_batches_are_serial_and_rotate_between_tens() -> None:
    products = [product(str(index)) for index in range(1, 12)]
    details = []
    for item in products:
        variant = item["variants"][0]
        identifier = variant["id"]
        variant.pop("inventory_quantity")
        variant.pop("inventory_management")
        variant.pop("inventory_policy")
        details.append(
            {
                "variants": [
                    {
                        "id": identifier,
                        "inventory_quantity": 4,
                        "inventory_management": "shopify",
                        "inventory_policy": "deny",
                    }
                ]
            }
        )
    fetcher = FakeFetcher([{"products": products}, *details])
    connector = ShopifyConnector(
        fetcher,
        ShopifyOptions(currency="EUR", inventory_method="product_json"),
        clock=lambda: NOW,
    )

    [page] = [page async for page in connector.collect(request())]

    assert all(
        item.variants[0].stock is not None and item.variants[0].stock.quantity == 4 for item in page.items
    )
    assert [call[0] for call in fetcher.calls].count("rotate") == 1


@pytest.mark.asyncio
async def test_html_inventory_is_normalized_as_exact_stock() -> None:
    item = product()
    variant = item["variants"][0]
    variant.pop("inventory_quantity")
    variant.pop("inventory_management")
    variant.pop("inventory_policy")
    document = (
        '<script>{"id":20,"inventory_quantity":6,'
        '"inventory_management":"shopify","inventory_policy":"deny"}</script>'
    )
    fetcher = FakeFetcher([{"products": [item]}, document])
    connector = ShopifyConnector(
        fetcher,
        ShopifyOptions(currency="EUR", inventory_method="product_html"),
        clock=lambda: NOW,
    )

    [page] = [page async for page in connector.collect(request())]
    stock = page.items[0].variants[0].stock
    assert stock is not None and stock.quantity == 6
