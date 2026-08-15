from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from mb_ceramics_catalogue.connectors import (
    CollectionRequest,
    ConnectorCheckpoint,
    NitroSellConnector,
    NitroSellOptions,
    RefreshMode,
    ShopwareConnector,
    ShopwareOptions,
    SnapshotField,
    StarwebConnector,
    StarwebOptions,
    SumUpConnector,
    SumUpOptions,
)
from mb_ceramics_catalogue.connectors.pagecommerce import ParserDisposition
from mb_ceramics_catalogue.datasets import CeramicsCatalogueProjector, ProjectionContext
from mb_ceramics_catalogue.scrapers.nitrosell import NitroSellScraper
from mb_ceramics_catalogue.scrapers.shopware import ShopwareScraper
from mb_ceramics_catalogue.scrapers.starweb import StarwebScraper
from mb_ceramics_catalogue.scrapers.sumup import SumUpScraper

NOW = datetime(2026, 8, 15, tzinfo=UTC)


class Transport:
    def __init__(self, documents):
        self.documents = documents
        self.calls = []

    async def advertised_sitemaps(self, base_url):
        del base_url
        return ()

    async def document(self, url, *, rendered=False, accept=None):
        self.calls.append((url, rendered, accept))
        del rendered, accept
        value = self.documents[url]
        if isinstance(value, Exception):
            raise value
        return value


def request() -> CollectionRequest:
    return CollectionRequest(
        source_id="shop", base_url="https://shop.test/", refresh_mode=RefreshMode.FULL,
        requested_fields=frozenset(SnapshotField),
    )


JSONLD = """<html><script type="application/ld+json">{
 "@type":"Product","name":"Clay","sku":"CL-1","image":"https://shop.test/a.jpg",
 "offers":{"price":"12.50","priceCurrency":"EUR","availability":"InStock"}
}</script></html>"""
SITEMAP = "<urlset><url><loc>https://shop.test/a</loc></url><url><loc>https://shop.test/b</loc></url></urlset>"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connector_type", "options_type"),
    [(ShopwareConnector, ShopwareOptions), (StarwebConnector, StarwebOptions)],
)
async def test_structured_specialized_connectors_are_ordered_and_resumable(
    connector_type, options_type
):
    transport = Transport({"https://shop.test/s.xml": SITEMAP,
                           "https://shop.test/a": JSONLD, "https://shop.test/b": JSONLD})
    connector = connector_type(
        transport, options_type(sitemaps=("https://shop.test/s.xml",), product_pattern=r"/[ab]$")
    )
    pages = [page async for page in connector.collect(request())]

    assert [page.sequence for page in pages] == [0, 1]
    assert [page.partition_key for page in pages] == ["sitemap", "sitemap"]
    assert pages[-1].terminal
    assert pages[0].resume_after == {"partition": "sitemap", "index": 1, "sequence": 1}

    resumed = [page async for page in connector.collect(
        request(), ConnectorCheckpoint(
            connector_version=connector.version,
            connector=connector.name, source_id="shop",
            lineage="00000000-0000-0000-0000-000000000001",
            resume_after=pages[0].resume_after,
        )
    )]
    assert len(resumed) == 1
    assert resumed[0].sequence == 1


@pytest.mark.asyncio
async def test_specialized_fetch_failure_is_typed_and_incomplete():
    connector = NitroSellConnector(
        Transport({"https://shop.test/s.xml": SITEMAP,
                   "https://shop.test/a": RuntimeError("blocked"),
                   "https://shop.test/b": ""}),
        NitroSellOptions(sitemaps=("https://shop.test/s.xml",), product_pattern=r"/[ab]$"),
    )
    page = await anext(connector.collect(request()))
    assert not page.enumeration_intact
    assert page.diagnostics[0].code == "entity_fetch_failed"


NITRO = """<html><head>
<meta property="og:title" content="Blue glaze"><meta property="og:upc" content="BG-1">
<meta property="product:price:amount" content="14.25">
<meta property="product:price:currency" content="USD">
<meta property="og:availability" content="instock">
</head></html>"""


def test_nitrosell_neutral_parse_preserves_offer_identity():
    connector = NitroSellConnector(Transport({}), NitroSellOptions(vat_status="exclusive"), clock=lambda: NOW)
    outcome = connector.parse(NITRO, "https://shop.test/product/blue", "shop", NOW)
    assert outcome.disposition == ParserDisposition.PARSED
    snapshot = outcome.snapshots[0]
    assert snapshot.title == "Blue glaze"
    assert snapshot.variants[0].sku == "BG-1"
    assert snapshot.variants[0].offers[0].price.amount == 14.25
    assert snapshot.variants[0].offers[0].vat_status == "exclusive"


def _sumup_page() -> str:
    product = {
        "id": "ebec5308-b80d-4c0c-ae84-26ce26a341b4", "name": "Tasse bleue",
        "slug": "tasse-bleue", "price": 2500, "basePrice": 3000,
        "hasDiscount": True, "isAvailable": True,
        "image": "https://images.test/one.jpg", "allImages": ["https://images.test/one.jpg"],
        "category": {"name": "Ceramiques"},
        "variants": {"1f7ac7e9-8bb0-4998-b88f-077b7a249862": {
            "uuid": "1f7ac7e9-8bb0-4998-b88f-077b7a249862", "price": 2500,
            "quantity": 3, "isAvailable": True, "isTrackingEnabled": True,
        }},
    }
    payload = '{"currency":"EUR","product":' + json.dumps(product, separators=(",", ":")) + "}"
    return f"<script>self.__next_f.push([1,{json.dumps(payload)}])</script>"


def test_sumup_rsc_parser_keeps_minor_units_and_exact_stock():
    connector = SumUpConnector(Transport({}), SumUpOptions(), clock=lambda: NOW)
    outcome = connector.parse(
        _sumup_page(), "https://shop.test/article/tasse-bleue", "shop", NOW
    )
    assert outcome.disposition == ParserDisposition.PARSED
    variant = outcome.snapshots[0].variants[0]
    assert variant.offers[0].price.amount == 25
    assert variant.stock is not None
    assert variant.stock.quantity == 3
    assert variant.stock.quantity_kind == "exact"


def _legacy(scraper_type, document: str, url: str, **config):
    fetcher = SimpleNamespace(
        limiter=SimpleNamespace(join_group=lambda *args: None, set_delay=lambda *args: None)
    )
    scraper = scraper_type(
        "shop", {"url": "https://shop.test/", "scope": "all", **config}, fetcher
    )
    return scraper.parse(document, url)[0][0]


def _project(snapshot, method: str):
    projector = CeramicsCatalogueProjector()
    context = ProjectionContext(
        collection_id="test", source_id="shop", dataset=projector.name,
        dataset_version=projector.version, projector_version=projector.projector_version,
        configuration={"scope": "all", "extraction_method": method,
                       "source_detail_level": "product_page", "apply_scope": False},
    )
    return next(iter(projector.project(snapshot, context))).as_legacy_dict()


def test_shopware_compatibility_fields_match_legacy_parser():
    document = JSONLD.replace("</html>", """
      <dl><dt class="properties-label">Firing:</dt><dd class="properties-value">1200 C</dd></dl>
      <span class="product-detail-ordernumber">CL-99</span>
      <span class="product-detail-price-unit">2.50 / kg</span>
      <input class="quantity-selector" name="quantity" max="7"></html>""")
    legacy = _legacy(ShopwareScraper, document, "https://shop.test/a")
    connector = ShopwareConnector(Transport({}), ShopwareOptions(currency="EUR"), clock=lambda: NOW)
    row = _project(connector.parse(document, "https://shop.test/a", "shop", NOW).snapshots[0], "dom")
    for field in ("supplier_reference", "published_unit_price", "stock_quantity"):
        assert row.get(field) == legacy.get(field)
    assert row["technical_attributes"]["Firing"] == legacy["technical_attributes"]["Firing"]


def test_starweb_vat_and_variant_markup_match_legacy_parser():
    document = JSONLD.replace("<html>", "<html class=\"incl-vat\">").replace(
        "</html>", '<label class="variant-name">Size:</label><span class="variant-value">1 kg</span></html>'
    )
    legacy = _legacy(StarwebScraper, document, "https://shop.test/a")
    connector = StarwebConnector(Transport({}), StarwebOptions(currency="EUR"), clock=lambda: NOW)
    row = _project(connector.parse(document, "https://shop.test/a", "shop", NOW).snapshots[0], "dom")
    assert row.get("vat_status") == legacy.get("vat_status")
    assert row.get("vat_basis") == legacy.get("vat_basis")
    assert row["technical_attributes"]["Size"] == legacy["technical_attributes"]["Size"]


def test_nitrosell_extended_page_fields_match_legacy_parser():
    document = NITRO.replace("</html>", """
      <strong class="priceCurrent">$14.25</strong><span class="text-pricestrike">$18.00</span>
      <ol class="breadcrumb"><li>Home</li><li>Glazes</li><li>Blue glaze</li></ol>
      <div class="product-description">Long blue description</div>
      <img src="https://cdn.powered-by-nitrosell.com/product_images/blue-large.jpg"></html>""")
    legacy = _legacy(NitroSellScraper, document, "https://shop.test/product/blue", vat_status="exclusive")
    connector = NitroSellConnector(
        Transport({}), NitroSellOptions(vat_status="exclusive"), clock=lambda: NOW
    )
    row = _project(
        connector.parse(document, "https://shop.test/product/blue", "shop", NOW).snapshots[0],
        "opengraph",
    )
    for field in ("supplier_reference", "description", "category_path", "list_price"):
        assert row.get(field) == legacy.get(field)
    assert row["all_image_urls"] == legacy["all_image_urls"]


def test_sumup_variant_compatibility_matches_legacy_parser():
    document = _sumup_page()
    legacy = _legacy(SumUpScraper, document, "https://shop.test/article/tasse-bleue")
    connector = SumUpConnector(Transport({}), SumUpOptions(), clock=lambda: NOW)
    row = _project(
        connector.parse(document, "https://shop.test/article/tasse-bleue", "shop", NOW).snapshots[0],
        "dom",
    )
    for field in ("product_name", "variant_id", "price", "currency", "stock_quantity"):
        assert row.get(field) == legacy.get(field)
    for field in ("list_price", "image_url", "all_image_urls", "category_path", "raw"):
        assert row.get(field) == legacy.get(field)


@pytest.mark.asyncio
async def test_sumup_collection_counts_one_request_and_one_discovered_product():
    sitemap = "<urlset><url><loc>https://shop.test/article/tasse-bleue</loc></url></urlset>"
    transport = Transport({
        "https://shop.test/sitemap.products.xml": sitemap,
        "https://shop.test/article/tasse-bleue": _sumup_page(),
    })
    connector = SumUpConnector(
        transport, SumUpOptions(sitemaps=("https://shop.test/sitemap.products.xml",)),
        clock=lambda: NOW,
    )
    pages = [page async for page in connector.collect(request())]
    assert len(pages) == 1
    assert pages[0].discovered == 1
    assert pages[0].terminal and pages[0].enumeration_intact
    assert [call[0] for call in transport.calls] == [
        "https://shop.test/sitemap.products.xml", "https://shop.test/article/tasse-bleue"
    ]
