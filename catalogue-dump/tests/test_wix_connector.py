from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest

from mb_ceramics_catalogue import scrapers
from mb_ceramics_catalogue.connectors import (
    BrowserBackendName,
    CollectionRequest,
    ConnectorCheckpoint,
    DiagnosticCode,
    RefreshMode,
    SnapshotField,
    StockQuantityKind,
    WixConnector,
    WixOptions,
)
from mb_ceramics_catalogue.datasets import CeramicsCatalogueProjector, ProjectionContext
from mb_ceramics_catalogue.pipeline.budget import RequestBudget, RequestCost

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
SITEMAP = "https://shop.test/store-products-sitemap.xml"


class Transport:
    def __init__(self, documents: dict[tuple[str, bool], Any], advertised=()) -> None:
        self.documents = dict(documents)
        self.advertised = tuple(advertised)
        self.calls: list[tuple[str, bool, str | None]] = []

    async def advertised_sitemaps(self, base_url: str) -> tuple[str, ...]:
        return self.advertised

    async def document(self, url: str, *, rendered=False, accept=None) -> str:
        self.calls.append((url, rendered, accept))
        value = self.documents[(url, rendered)]
        if isinstance(value, Exception):
            raise value
        return value


def request(*, limit: int | None = None) -> CollectionRequest:
    return CollectionRequest(
        source_id="wix-shop",
        base_url="https://shop.test/",
        refresh_mode=RefreshMode.FULL,
        requested_fields=frozenset(SnapshotField),
        result_limit=limit,
    )


def sitemap(*urls: str) -> str:
    return "<urlset>" + "".join(f"<url><loc>{url}</loc></url>" for url in urls) + "</urlset>"


def product_page(slug: str = "glaze", identifier: str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa") -> str:
    product = {
        "id": identifier,
        "name": "Transparent <b>Glaze</b>",
        "description": "A glossy &amp; durable glaze.",
        "brand": "Test Ceramics",
        "sku": "PARENT",
        "price": 12.5,
        "comparePrice": 15,
        "formattedPrice": "€12.50",
        "isInStock": True,
        "isTrackingInventory": True,
        "inventory": {"quantity": 7, "status": "in_stock"},
        "media": [{"fullUrl": "https://cdn.test/fallback.jpg"}],
        "productItems": [
            {
                "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "sku": "GL-500",
                "price": 11,
                "comparePrice": 14,
                "formattedPrice": "€11.00",
                "isInStock": True,
                "isTrackingInventory": True,
                "inventory": {"quantity": 3},
                "optionsSelections": {"Size": "500 ml"},
            },
            {
                "id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "sku": "GL-1K",
                "price": 20,
                "comparePrice": 0,
                "formattedPrice": "€20.00",
                "isInStock": False,
                "inventory": {"status": "out_of_stock", "quantity": 99},
                "optionsSelections": {"Size": "1 kg"},
            },
        ],
    }
    jsonld = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "Product",
                "name": "JSON-LD name",
                "image": ["https://cdn.test/published.jpg"],
            },
            {
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {"item": {"name": "Store"}},
                    {"item": {"name": "Glazes"}},
                ],
            },
        ],
    }
    return (
        f'<script type="application/ld+json">{json.dumps(jsonld)}</script>'
        '<script>window.warmup={"'
        + slug
        + '":{"product":'
        + json.dumps(product)
        + "}};</script>"
        '<script>const locale={"currency":"EUR"};</script>'
        '<a href="/docs/sds.pdf">Safety data sheet</a>'
    )


@pytest.mark.asyncio
async def test_browser_fallback_budget_exhaustion_is_typed_without_exceeding_ceiling() -> None:
    url = "https://shop.test/product-page/glaze"
    transport = Transport({(SITEMAP, False): sitemap(url), (url, False): "<html></html>"})
    budget = RequestBudget(RequestCost(http_requests=2, browser_requests=0))
    connector = WixConnector(
        transport,
        WixOptions(sitemaps=(SITEMAP,), use_advertised_sitemaps=False),
        budget=budget,
    )

    [page] = [item async for item in connector.collect(request())]

    assert page.terminal and not page.enumeration_intact
    assert page.resume_after == {"index": 0, "sequence": 0}
    assert page.diagnostics[0].code == DiagnosticCode.REQUEST_BUDGET_EXHAUSTED
    assert budget.used == RequestCost(http_requests=2)
    assert all(not rendered for _, rendered, _ in transport.calls)


@pytest.mark.asyncio
async def test_warmup_payload_emits_neutral_published_price_stock_and_documents() -> None:
    url = "https://shop.test/product-page/glaze"
    transport = Transport({(SITEMAP, False): sitemap(url), (url, False): product_page()})
    connector = WixConnector(
        transport,
        WixOptions(sitemaps=(SITEMAP,), render=False, vat_status="inclusive"),
        clock=lambda: NOW,
    )

    [page] = [item async for item in connector.collect(request())]

    assert page.terminal and page.enumeration_intact
    snapshot = page.items[0]
    assert snapshot.title == "Transparent Glaze"
    assert [category.name for category in snapshot.categories] == ["Store", "Glazes"]
    assert snapshot.images[0].url == "https://cdn.test/published.jpg"
    assert snapshot.documents[0].url == "https://shop.test/docs/sds.pdf"
    assert snapshot.documents[0].evidence[0].method == "html"
    first, second = snapshot.variants
    assert first.options == {"Size": "500 ml"}
    assert [offer.role for offer in first.offers] == ["sale", "regular"]
    assert [offer.price.amount for offer in first.offers] == [11, 14]
    assert first.stock is not None and first.stock.quantity == 3
    assert first.stock.quantity_kind == StockQuantityKind.EXACT
    assert second.stock is not None and second.stock.quantity == 0


@pytest.mark.asyncio
async def test_checkpoint_resume_uses_declared_index_not_url_lexical_order() -> None:
    urls = (
        "https://shop.test/product-page/z-last",
        "https://shop.test/product-page/a-first",
    )
    documents = {
        (SITEMAP, False): sitemap(*urls),
        (urls[0], False): product_page("z-last", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        (urls[1], False): product_page("a-first", "dddddddd-dddd-dddd-dddd-dddddddddddd"),
    }
    first_connector = WixConnector(
        Transport(documents), WixOptions(sitemaps=(SITEMAP,), render=False), clock=lambda: NOW
    )
    pages = [page async for page in first_connector.collect(request())]
    checkpoint = ConnectorCheckpoint(
        connector="wix",
        connector_version="1",
        source_id="wix-shop",
        lineage="replay",
        resume_after=pages[0].resume_after,
    )
    resumed_transport = Transport(documents)
    resumed = WixConnector(
        resumed_transport, WixOptions(sitemaps=(SITEMAP,), render=False), clock=lambda: NOW
    )

    [page] = [item async for item in resumed.collect(request(), checkpoint)]

    assert page.items[0].canonical_url == urls[1]
    assert (urls[0], False, None) not in resumed_transport.calls
    assert page.model_dump_json() == pages[1].model_dump_json()


@pytest.mark.asyncio
async def test_result_and_operator_page_limits_have_distinct_completion_semantics() -> None:
    urls = tuple(f"https://shop.test/product-page/p-{index}" for index in range(3))
    documents = {(SITEMAP, False): sitemap(*urls)}
    documents.update(
        {
            (url, False): product_page(f"p-{index}", f"{index + 1:08x}-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
            for index, url in enumerate(urls)
        }
    )
    limited = WixConnector(
        Transport(documents), WixOptions(sitemaps=(SITEMAP,), render=False), clock=lambda: NOW
    )
    [result_page] = [page async for page in limited.collect(request(limit=1))]
    bounded = WixConnector(
        Transport(documents),
        WixOptions(sitemaps=(SITEMAP,), render=False, page_limit=1),
        clock=lambda: NOW,
    )
    bounded_pages = [page async for page in bounded.collect(request())]

    assert result_page.terminal and not result_page.enumeration_intact
    assert result_page.resume_after == {"index": 1, "sequence": 1}
    assert result_page.diagnostics[0].code == DiagnosticCode.RESULT_LIMIT_REACHED
    assert not bounded_pages[-1].enumeration_intact
    assert bounded_pages[-1].resume_after == {"index": 1, "sequence": 1}
    assert bounded_pages[-1].diagnostics[0].code == DiagnosticCode.ENUMERATION_INCOMPLETE


@pytest.mark.asyncio
async def test_render_fallback_is_backend_neutral_and_never_leaks_document_content() -> None:
    url = "https://shop.test/product-page/glaze"
    secret = "private-warmup-token"
    transport = Transport(
        {
            (SITEMAP, False): sitemap(url),
            (url, False): "<html><div id='root'></div></html>",
            (url, True): product_page() + secret,
        }
    )
    connector = WixConnector(
        transport, WixOptions(sitemaps=(SITEMAP,)), clock=lambda: NOW
    )

    [page] = [item async for item in connector.collect(request())]

    assert page.enumeration_intact
    assert (url, True, None) in transport.calls
    assert secret not in page.model_dump_json()
    assert WixConnector.capabilities.browser_backends == {
        BrowserBackendName.CAMOUFOX,
        BrowserBackendName.CDP_EXTENSION_PROXY,
    }


@pytest.mark.asyncio
async def test_projector_preserves_legacy_parity_critical_fields() -> None:
    url = "https://shop.test/product-page/glaze"
    connector = WixConnector(
        Transport({(SITEMAP, False): sitemap(url), (url, False): product_page()}),
        WixOptions(sitemaps=(SITEMAP,), render=False, vat_status="inclusive"),
        clock=lambda: NOW,
    )
    [page] = [item async for item in connector.collect(request())]
    projector = CeramicsCatalogueProjector()
    context = ProjectionContext(
        collection_id="test",
        source_id="wix-shop",
        dataset=projector.name,
        dataset_version=projector.version,
        projector_version=projector.projector_version,
        configuration={
            "scope": "all",
            "apply_scope": False,
            "extraction_method": "dom",
            "source_detail_level": "product_page",
        },
    )

    rows = [row.as_legacy_dict() for row in projector.project(page.items[0], context)]

    assert rows[0]["price"] == 11.0
    assert rows[0]["list_price"] == 14.0
    assert rows[0]["stock_quantity"] == 3
    assert rows[0]["price_text"] == "€11.00"
    assert rows[0]["availability"] == "https://schema.org/InStock"
    documents = cast(list[dict[str, Any]], rows[0]["documents"])
    raw = cast(dict[str, Any], rows[0]["raw"])
    assert documents[0]["url"] == "https://shop.test/docs/sds.pdf"
    assert "productItems" not in raw["product"]
    assert rows[1]["stock_quantity"] == 0


@pytest.mark.asyncio
async def test_fetch_failure_is_replayable_and_canary_is_explicit() -> None:
    url = "https://shop.test/product-page/glaze"
    response = httpx.Response(503, request=httpx.Request("GET", url))
    transport = Transport(
        {(SITEMAP, False): sitemap(url), (url, False): httpx.HTTPStatusError("down", request=response.request, response=response)}
    )
    connector = WixConnector(
        transport, WixOptions(sitemaps=(SITEMAP,), render=False), clock=lambda: NOW
    )

    [page] = [item async for item in connector.collect(request())]

    assert not page.enumeration_intact
    assert page.resume_after == {"index": 0, "sequence": 0}
    assert page.diagnostics[0].code == DiagnosticCode.ENTITY_FETCH_FAILED
    assert scrapers.REGISTRY["wix"] == ".wix:WixScraper"
    assert scrapers.REGISTRY["wix_connector"] == ".wix_connector:WixConnectorScraper"
