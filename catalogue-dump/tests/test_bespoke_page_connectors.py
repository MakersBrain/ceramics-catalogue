from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest

from mb_ceramics_catalogue import scrapers
from mb_ceramics_catalogue.connectors import (
    AxnerConnector,
    AxnerOptions,
    CeramicoloursConnector,
    CeramicoloursOptions,
    CollectionRequest,
    ConnectorCheckpoint,
    DiagnosticCode,
    KeramikKraftConnector,
    KeramikKraftOptions,
    RefreshMode,
    SnapshotField,
    StockQuantityKind,
)
from mb_ceramics_catalogue.datasets import CeramicsCatalogueProjector, ProjectionContext

NOW = datetime(2026, 8, 15, 16, 0, tzinfo=UTC)


class Transport:
    def __init__(self, documents: dict[tuple[str, bool], Any], evaluations=()) -> None:
        self.documents = dict(documents)
        self.evaluations = list(evaluations)
        self.calls: list[tuple[str, bool]] = []

    async def advertised_sitemaps(self, base_url: str) -> tuple[str, ...]:
        return ()

    async def document(self, url: str, *, rendered=False, accept=None) -> str:
        self.calls.append((url, rendered))
        value = self.documents[(url, rendered)]
        if isinstance(value, Exception):
            raise value
        return value

    async def evaluate(self, url: str, script: str, *, wait_for=None):
        value = self.evaluations.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def request(source: str, *, limit: int | None = None) -> CollectionRequest:
    return CollectionRequest(
        source_id=source,
        base_url=f"https://{source}.test/",
        refresh_mode=RefreshMode.FULL,
        requested_fields=frozenset(SnapshotField),
        result_limit=limit,
    )


def axner_product(reference: str = "AX-1") -> str:
    return f"""
    <h1>Blue Stoneware</h1>
    <span class="product-list-cost-value">$ 12.50</span>
    <span class="prod-detail-man-name-value">Axner</span>
    <div class="prod-detail-desc">Strong &amp; plastic clay</div>
    <span class="prod-detail-part-label">Axner Number:</span><span class="prod-detail-part-value">{reference}</span>
    <span class="prod-detail-part-label">Cone:</span><span class="prod-detail-part-value">6</span>
    <img src="/ProductImages/clay.jpg"><a href="/docs/sds.pdf">Safety data sheet</a>
    """


@pytest.mark.asyncio
async def test_axner_discovery_resume_and_published_evidence() -> None:
    base = "https://axner.test/"
    index = f"{base}sitemap.aspx"
    department = f"{base}glazes.aspx"
    urls = (f"{base}z-product.aspx", f"{base}a-product.aspx")
    documents = {
        (index, False): '<a href="/glazes.aspx">Glazes</a>',
        (department, False): "".join(
            f'<h5 class="category-page product-list-link"><a href="{url}">item</a></h5>'
            for url in urls
        ),
        (urls[0], False): axner_product("Z-1"),
        (urls[1], False): axner_product("A-1"),
    }
    connector = AxnerConnector(
        Transport(documents),
        AxnerOptions(category_url=index, render=False, vat_status="exclusive"),
        clock=lambda: NOW,
    )
    pages = [page async for page in connector.collect(request("axner"))]
    checkpoint = ConnectorCheckpoint(
        connector="axner",
        connector_version="1",
        source_id="axner",
        lineage="replay",
        resume_after=pages[0].resume_after,
    )
    replay_transport = Transport(documents)
    replay = AxnerConnector(replay_transport, connector.options, clock=lambda: NOW)

    [resumed] = [page async for page in replay.collect(request("axner"), checkpoint)]

    snapshot = pages[0].items[0]
    variant = snapshot.variants[0]
    assert snapshot.canonical_url == urls[0]
    assert resumed.items[0].canonical_url == urls[1]
    assert (urls[0], False) not in replay_transport.calls
    assert variant.offers[0].price.amount == Decimal("12.50")
    assert variant.offers[0].evidence[0].source_field == "axner_data_item"
    assert snapshot.documents[0].url == f"{base}docs/sds.pdf"
    assert variant.published_attributes["Cone"] == "6"


@pytest.mark.asyncio
async def test_bespoke_result_limit_is_incomplete_and_resumable() -> None:
    base = "https://axner.test/"
    index, department = f"{base}sitemap.aspx", f"{base}glazes.aspx"
    urls = (f"{base}one.aspx", f"{base}two.aspx")
    documents = {
        (index, False): '<a href="/glazes.aspx">Glazes</a>',
        (department, False): "".join(
            f'<h5 class="product-list-link"><a href="{url}">item</a></h5>' for url in urls
        ),
        (urls[0], False): axner_product("ONE"),
        (urls[1], False): axner_product("TWO"),
    }
    connector = AxnerConnector(
        Transport(documents), AxnerOptions(category_url=index, render=False), clock=lambda: NOW
    )

    [limited] = [page async for page in connector.collect(request("axner", limit=1))]

    assert limited.terminal and not limited.enumeration_intact
    assert limited.resume_after == {"partition": "main", "index": 1, "sequence": 1}
    assert limited.diagnostics[0].code == DiagnosticCode.RESULT_LIMIT_REACHED


def ceramicolours_documents() -> tuple[dict[tuple[str, bool], Any], str]:
    base = "https://ceramicolours.test/"
    category = f"{base}Articoli.php?Id=5101"
    product = f"{base}Articolo.php?cod=BLU1"
    documents = {
        (base, False): '<a href="Articoli.php?Id=5101">Glazes</a>',
        (f"{category}&page=1", False): '<a href="Articolo.php?cod=BLU1" class="product-name">Blue</a>',
        (f"{category}&page=2", False): "<p>end</p>",
        (product, False): """
            <h1>Blu Cobalto</h1><select id="product-pack-field"></select>
            <p><span>Temp.</span> 1020-1080 °C</p>
            <input id="icaOrdinabile" value="10"><div class="product-description">Blue glaze</div>
            <img src="upload-immagini/blu.jpg">
        """,
    }
    return documents, product


@pytest.mark.asyncio
async def test_ceramicolours_browser_pack_prices_are_not_derived_and_stock_is_exact() -> None:
    documents, product = ceramicolours_documents()
    packs = [
        {"pack": "1", "value": "1", "price": "26,65 €", "unit_price": "26,65 €/kg"},
        {"pack": "5", "value": "5", "price": "99,00 €", "unit_price": "19,80 €/kg"},
    ]
    connector = CeramicoloursConnector(
        Transport(documents, [packs]),
        CeramicoloursOptions(category_ids=("5101",)),
        clock=lambda: NOW,
    )

    [page] = [item async for item in connector.collect(request("ceramicolours"))]

    snapshot = page.items[0]
    first, second = snapshot.variants
    assert snapshot.canonical_url == product
    assert [item.offers[0].price.amount for item in snapshot.variants] == [
        Decimal("26.65"),
        Decimal("99.00"),
    ]
    assert first.stock is not None and first.stock.quantity == 10
    assert second.stock is not None and second.stock.quantity == 2
    assert first.stock.quantity_kind == StockQuantityKind.EXACT
    assert snapshot.platform_extensions["page_parser"] == "browser"


@pytest.mark.asyncio
async def test_ceramicolours_browser_degrades_to_static_published_price() -> None:
    documents, _ = ceramicolours_documents()
    product_key = next(key for key in documents if "Articolo.php" in key[0])
    documents[product_key] = str(documents[product_key]).replace(
        "</select>", "</select><p><span>Prezzo:</span> 8,40 €</p>"
    )
    connector = CeramicoloursConnector(
        Transport(documents, [RuntimeError("browser unavailable")]),
        CeramicoloursOptions(category_ids=("5101",)),
        clock=lambda: NOW,
    )

    [page] = [item async for item in connector.collect(request("ceramicolours"))]

    assert page.items[0].variants[0].offers[0].price.amount == Decimal("8.40")
    assert page.items[0].platform_extensions["page_parser"] == "dom"


def kraft_card() -> str:
    return """
    <div class="product card">
      <p class="text-sm">Mayco Blue Glaze<br>Bt. á 0,25 kg</p>
      <p class="p mb-1">MAY-1</p>
      <span>4,97 € <i>(4,18 € HT)</i></span>
      <a href="Mayco-Blue_Bt.-a-0.25-kg_MAY-1.html">detail</a><img src="/blue.jpg">
    <!-- /product
    """


@pytest.mark.asyncio
async def test_keramik_kraft_listing_card_preserves_gross_net_and_listing_evidence() -> None:
    base = "https://keramik_kraft.test/"
    category = f"{base}de/Glasuren.html"
    connector = KeramikKraftConnector(
        Transport({(category, False): kraft_card()}),
        KeramikKraftOptions(category_paths=("de/Glasuren.html",), vat_rate=Decimal("0.19")),
        clock=lambda: NOW,
    )

    [page] = [item async for item in connector.collect(request("keramik_kraft"))]

    snapshot = page.items[0]
    variant = snapshot.variants[0]
    assert variant.offers[0].price.amount == Decimal("4.97")
    assert variant.offers[0].vat_status == "inclusive"
    assert variant.published_attributes["Netto-Preis EUR"] == 4.18
    assert variant.stock is not None and variant.stock.availability.value == "in_stock"
    assert variant.stock.quantity is None
    assert variant.offers[0].evidence[0].source_url == category
    assert snapshot.vendor == "Mayco"


@pytest.mark.asyncio
async def test_bespoke_bounds_failures_and_canaries_are_explicit() -> None:
    base = "https://axner.test/"
    index = f"{base}sitemap.aspx"
    department = f"{base}glazes.aspx"
    response = httpx.Response(503, request=httpx.Request("GET", department))
    connector = AxnerConnector(
        Transport(
            {
                (index, False): '<a href="/glazes.aspx">Glazes</a>',
                (department, False): httpx.HTTPStatusError(
                    "down", request=response.request, response=response
                ),
            }
        ),
        AxnerOptions(category_url=index, render=False),
    )

    [page] = [item async for item in connector.collect(request("axner"))]

    assert not page.enumeration_intact
    assert page.diagnostics[0].code == DiagnosticCode.ENUMERATION_INCOMPLETE
    assert page.resume_after == {"partition": "main", "index": 0, "sequence": 0}
    assert scrapers.REGISTRY["axner"] == ".axner:AxnerScraper"
    assert scrapers.REGISTRY["axner_connector"].endswith(":AxnerConnectorScraper")
    assert scrapers.REGISTRY["ceramicolours"] == ".ceramicolours:CeramicoloursScraper"
    assert scrapers.REGISTRY["ceramicolours_connector"].endswith(
        ":CeramicoloursConnectorScraper"
    )
    assert scrapers.REGISTRY["keramik_kraft"] == ".keramik_kraft:KeramikKraftScraper"
    assert scrapers.REGISTRY["keramik_kraft_connector"].endswith(
        ":KeramikKraftConnectorScraper"
    )


@pytest.mark.asyncio
async def test_axner_product_bound_emits_numeric_resume_cursor() -> None:
    base = "https://axner.test/"
    index, department = f"{base}sitemap.aspx", f"{base}clay.aspx"
    urls = (f"{base}z.aspx", f"{base}a.aspx")
    connector = AxnerConnector(
        Transport(
            {
                (index, False): '<a href="/clay.aspx">Clay</a>',
                (department, False): "".join(
                    f'<h5 class="product-list-link"><a href="{url}">item</a></h5>'
                    for url in urls
                ),
                (urls[0], False): axner_product("Z"),
            }
        ),
        AxnerOptions(category_url=index, render=False, page_limit=1),
        clock=lambda: NOW,
    )

    pages = [page async for page in connector.collect(request("axner"))]

    assert pages[0].items[0].canonical_url == urls[0]
    assert not pages[-1].enumeration_intact
    assert pages[-1].resume_after == {"partition": "main", "index": 1, "sequence": 1}
    assert pages[-1].diagnostics[0].code == DiagnosticCode.ENUMERATION_INCOMPLETE


@pytest.mark.asyncio
async def test_axner_ceramics_projection_preserves_price_reference_documents_and_raw() -> None:
    base = "https://axner.test/"
    index, department, product = (
        f"{base}sitemap.aspx",
        f"{base}clay.aspx",
        f"{base}blue-clay.aspx",
    )
    connector = AxnerConnector(
        Transport(
            {
                (index, False): '<a href="/clay.aspx">Clay</a>',
                (department, False): '<h5 class="product-list-link"><a href="/blue-clay.aspx">Blue</a></h5>',
                (product, False): axner_product(),
            }
        ),
        AxnerOptions(category_url=index, render=False, vat_status="exclusive"),
        clock=lambda: NOW,
    )
    [page] = [item async for item in connector.collect(request("axner"))]
    projector = CeramicsCatalogueProjector()
    context = ProjectionContext(
        collection_id="test",
        source_id="axner",
        dataset=projector.name,
        dataset_version=projector.version,
        projector_version=projector.projector_version,
        configuration={
            "scope": "all",
            "apply_scope": False,
            "extraction_method": "html",
            "source_detail_level": "product_page",
        },
    )

    [row] = [value.as_legacy_dict() for value in projector.project(page.items[0], context)]
    raw = cast(dict[str, Any], row["raw"])

    assert row["price"] == 12.5
    assert row["supplier_reference"] == "AX-1"
    assert cast(list[dict[str, Any]], row["documents"])[0]["url"].endswith("sds.pdf")
    assert raw["details"]["Cone"] == "6"
