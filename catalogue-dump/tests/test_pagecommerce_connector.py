from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest

from mb_ceramics_catalogue import scrapers
from mb_ceramics_catalogue.connectors import (
    CollectionRequest,
    ConnectorCheckpoint,
    DiagnosticCode,
    DomFieldSelector,
    PageCommerceConnector,
    PageCrawlOptions,
    ParserDisposition,
    RefreshMode,
    SnapshotField,
    StockQuantityKind,
    VerifiedDomRules,
)
from mb_ceramics_catalogue.datasets import CeramicsCatalogueProjector, ProjectionContext

NOW = datetime(2026, 8, 15, 14, 0, tzinfo=UTC)
SITEMAP = "https://shop.test/sitemap.xml"


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
        source_id="page-shop",
        base_url="https://shop.test/",
        refresh_mode=RefreshMode.FULL,
        requested_fields=frozenset(SnapshotField),
        result_limit=limit,
    )


def sitemap(*urls: str) -> str:
    return "<urlset>" + "".join(f"<url><loc>{url}</loc></url>" for url in urls) + "</urlset>"


def jsonld_page(identifier: str = "GL-500") -> str:
    product = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "Transparent &amp; Gloss Glaze",
        "description": "Durable glaze",
        "sku": identifier,
        "brand": {"name": "Test Ceramics"},
        "image": ["/images/glaze.jpg"],
        "offers": {
            "price": "12,50",
            "priceCurrency": "EUR",
            "availability": "https://schema.org/InStock",
            "inventoryLevel": {"value": 8},
        },
    }
    breadcrumb = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [{"item": {"name": "Glazes"}}],
    }
    return (
        f'<script type="application/ld+json">{json.dumps(product)}</script>'
        f'<script type="application/ld+json">{json.dumps(breadcrumb)}</script>'
        '<table><tr><th>Finish</th><td>Gloss</td></tr></table>'
        '<a href="/docs/sds.pdf">SDS</a>'
    )


@pytest.mark.asyncio
async def test_sitemap_jsonld_projects_parity_critical_fields_and_evidence() -> None:
    url = "https://shop.test/product/glaze"
    connector = PageCommerceConnector(
        Transport({(SITEMAP, False): sitemap(url), (url, False): jsonld_page()}),
        PageCrawlOptions(
            sitemaps=(SITEMAP,), product_pattern=r"^/product/", render=False, vat_status="inclusive"
        ),
        clock=lambda: NOW,
    )

    [page] = [item async for item in connector.collect(request())]

    snapshot = page.items[0]
    variant = snapshot.variants[0]
    assert page.partition_key == "sitemap" and page.terminal
    assert snapshot.title == "Transparent & Gloss Glaze"
    assert snapshot.categories[0].name == "Glazes"
    assert snapshot.documents[0].url == "https://shop.test/docs/sds.pdf"
    assert variant.offers[0].price.amount == 12.5
    assert variant.offers[0].evidence[0].method == "jsonld"
    assert variant.stock is not None and variant.stock.quantity == 8
    assert variant.stock.quantity_kind == StockQuantityKind.EXACT
    assert variant.published_attributes["Finish"] == "Gloss"


@pytest.mark.asyncio
async def test_category_pagination_preserves_declared_order_and_resume_index() -> None:
    category = "https://shop.test/category"
    second = "https://shop.test/category?page=2"
    urls = ("https://shop.test/product/z", "https://shop.test/product/a")
    documents = {
        (category, False): f'<a href="{urls[0]}">Z</a><a href="{second}">next</a>',
        (second, False): f'<a href="{urls[1]}">A</a>',
        (urls[0], False): jsonld_page("Z"),
        (urls[1], False): jsonld_page("A"),
    }
    first = PageCommerceConnector(
        Transport(documents),
        PageCrawlOptions(
            use_advertised_sitemaps=False,
            category_urls=(category,),
            product_pattern=r"^/product/",
            render=False,
        ),
        clock=lambda: NOW,
    )
    pages = [page async for page in first.collect(request())]
    checkpoint = ConnectorCheckpoint(
        connector="pagecommerce",
        connector_version="1",
        source_id="page-shop",
        lineage="replay",
        resume_after=pages[0].resume_after,
    )
    replay_transport = Transport(documents)
    replay = PageCommerceConnector(
        replay_transport,
        first.options,
        clock=lambda: NOW,
    )

    [resumed] = [page async for page in replay.collect(request(), checkpoint)]

    assert [page.items[0].canonical_url for page in pages] == list(urls)
    assert resumed.model_dump_json() == pages[1].model_dump_json()
    assert (urls[0], False, None) not in replay_transport.calls
    assert resumed.partition_key == "category"


def test_parser_outcomes_distinguish_unsupported_markup_from_browser_need() -> None:
    connector = PageCommerceConnector(Transport({}), PageCrawlOptions())

    unsupported = connector.parse("<html><p>ordinary article</p></html>", "https://shop.test/x", "s", NOW)
    browser = connector.parse(
        '<html><div id="root"></div><script src="app.js"></script></html>',
        "https://shop.test/x",
        "s",
        NOW,
    )

    assert unsupported.disposition == ParserDisposition.UNSUPPORTED
    assert browser.disposition == ParserDisposition.BROWSER_REQUIRED


@pytest.mark.asyncio
async def test_browser_is_used_only_for_typed_browser_required_outcome() -> None:
    url = "https://shop.test/product/glaze"
    transport = Transport(
        {
            (SITEMAP, False): sitemap(url),
            (url, False): '<html><div id="root"></div><script>start()</script></html>',
            (url, True): jsonld_page(),
        }
    )
    connector = PageCommerceConnector(
        transport,
        PageCrawlOptions(sitemaps=(SITEMAP,), product_pattern=r"^/product/"),
        clock=lambda: NOW,
    )

    [page] = [item async for item in connector.collect(request())]

    assert page.enumeration_intact
    assert (url, True, None) in transport.calls


def test_microdata_opengraph_and_verified_dom_strategies() -> None:
    connector = PageCommerceConnector(
        Transport({}), PageCrawlOptions(currency="EUR", stock_from_quantity_maximum=True)
    )
    microdata = """
    <div itemscope itemtype="https://schema.org/Product">
      <h1 itemprop="name">Stoneware Clay</h1><meta itemprop="sku" content="CLAY-1">
      <meta itemprop="price" content="9.50"><meta itemprop="priceCurrency" content="EUR">
      <link itemprop="availability" href="https://schema.org/InStock">
    </div><input name="quantity" max="6">
    """
    opengraph = """
    <meta property="og:title" content="Red Stain">
    <meta property="product:price:amount" content="7.25">
    <meta property="product:price:currency" content="EUR">
    <meta property="og:availability" content="outofstock">
    """
    rules = VerifiedDomRules(
        verification=(DomFieldSelector(selector=".product-page"),),
        name=DomFieldSelector(selector="h1.name"),
        price=DomFieldSelector(selector="span.price"),
        currency=DomFieldSelector(selector="[data-currency]", attribute="data-currency"),
        sku=DomFieldSelector(selector="#sku"),
    )
    dom_connector = PageCommerceConnector(
        Transport({}), PageCrawlOptions(parsers=("dom",), dom_rules=rules)
    )
    dom = """
    <main class="product-page"><h1 class="name">Blue Slip</h1>
    <span class="price">4.20</span><i data-currency="EUR"></i><span id="sku">SLIP-B</span></main>
    """

    micro = connector.parse(microdata, "https://shop.test/p/clay", "s", NOW)
    graph = connector.parse(opengraph, "https://shop.test/p/stain", "s", NOW)
    selected = dom_connector.parse(dom, "https://shop.test/p/slip", "s", NOW)

    assert micro.disposition == ParserDisposition.PARSED
    micro_stock = micro.snapshots[0].variants[0].stock
    assert micro_stock is not None and micro_stock.quantity_kind == StockQuantityKind.ORDER_LIMIT
    assert graph.disposition == ParserDisposition.PARSED
    graph_stock = graph.snapshots[0].variants[0].stock
    assert graph_stock is not None and graph_stock.availability.value == "out_of_stock"
    assert selected.disposition == ParserDisposition.PARSED
    assert selected.snapshots[0].variants[0].offers[0].price.amount == Decimal("4.20")


@pytest.mark.asyncio
async def test_bounds_failures_and_canary_are_explicit() -> None:
    urls = ("https://shop.test/product/one", "https://shop.test/product/two")
    documents = {
        (SITEMAP, False): sitemap(*urls),
        (urls[0], False): jsonld_page("ONE"),
    }
    connector = PageCommerceConnector(
        Transport(documents),
        PageCrawlOptions(sitemaps=(SITEMAP,), product_pattern=r"^/product/", render=False, page_limit=1),
        clock=lambda: NOW,
    )

    pages = [page async for page in connector.collect(request())]

    assert not pages[-1].enumeration_intact
    assert pages[-1].diagnostics[0].code == DiagnosticCode.ENUMERATION_INCOMPLETE
    assert pages[-1].resume_after == {"partition": "sitemap", "index": 1, "sequence": 1}
    assert scrapers.REGISTRY["pagecrawl"] == ".pagecrawl:PageScraper"
    assert scrapers.REGISTRY["pagecrawl_connector"] == ".pagecrawl_connector:PageCrawlConnectorScraper"


@pytest.mark.asyncio
async def test_projector_keeps_pagecrawl_price_stock_documents_and_raw_shape() -> None:
    url = "https://shop.test/product/glaze"
    connector = PageCommerceConnector(
        Transport({(SITEMAP, False): sitemap(url), (url, False): jsonld_page()}),
        PageCrawlOptions(sitemaps=(SITEMAP,), product_pattern=r"^/product/", render=False),
        clock=lambda: NOW,
    )
    [page] = [item async for item in connector.collect(request())]
    projector = CeramicsCatalogueProjector()
    context = ProjectionContext(
        collection_id="test",
        source_id="page-shop",
        dataset=projector.name,
        dataset_version=projector.version,
        projector_version=projector.projector_version,
        configuration={
            "scope": "all",
            "apply_scope": False,
            "extraction_method": "jsonld",
            "source_detail_level": "product_page",
        },
    )

    [row] = [value.as_legacy_dict() for value in projector.project(page.items[0], context)]
    raw = cast(dict[str, Any], row["raw"])

    assert row["price"] == 12.5
    assert row["stock_quantity"] == 8
    assert cast(list[dict[str, Any]], row["documents"])[0]["url"].endswith("sds.pdf")
    assert raw["sku"] == "GL-500"


@pytest.mark.asyncio
async def test_fetch_failure_is_typed_and_resume_safe() -> None:
    url = "https://shop.test/product/glaze"
    response = httpx.Response(503, request=httpx.Request("GET", url))
    connector = PageCommerceConnector(
        Transport(
            {
                (SITEMAP, False): sitemap(url),
                (url, False): httpx.HTTPStatusError(
                    "down", request=response.request, response=response
                ),
            }
        ),
        PageCrawlOptions(sitemaps=(SITEMAP,), product_pattern=r"^/product/", render=False),
    )

    [page] = [item async for item in connector.collect(request())]

    assert not page.enumeration_intact
    assert page.diagnostics[0].code == DiagnosticCode.ENTITY_FETCH_FAILED
    assert page.resume_after == {"partition": "sitemap", "index": 0, "sequence": 0}
