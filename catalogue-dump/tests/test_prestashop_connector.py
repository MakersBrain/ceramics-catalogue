from __future__ import annotations

import html
import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from mb_ceramics_catalogue.connectors import (
    CollectionRequest,
    ConnectorCheckpoint,
    DiagnosticCode,
    PrestaShopConnector,
    PrestaShopOptions,
    RefreshMode,
    SnapshotField,
    StockQuantityKind,
)
from mb_ceramics_catalogue.pipeline.budget import RequestBudget, RequestCost

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def details(*, attribute_id: int = 11, price: str = "€12,50", quantity: int = 4) -> dict[str, Any]:
    return {
        "id_product": 7,
        "id_product_attribute": attribute_id,
        "name": "Transparent Glaze",
        "link": "https://shop.test/product/7-glaze.html",
        "reference": "PARENT",
        "ean13": "1234567890123",
        "price": price,
        "price_amount": 12.5 if attribute_id == 11 else 18,
        "regular_price_amount": 15 if attribute_id == 11 else 18,
        "quantity": quantity,
        "manufacturer_name": "Test Ceramics",
        "description": "Gloss glaze",
        "category_name": "Glazes",
        "date_upd": "2026-08-14 10:30:00",
        "features": [{"name": "Finish", "value": "Gloss"}],
        "images": [{"large": {"url": "https://cdn.test/glaze.jpg"}}],
        "attachments": [{"id_attachment": 9, "file_name": "SDS"}],
        "attributes": {
            "1": {
                "group": "Size",
                "name": "500 ml" if attribute_id == 11 else "1 L",
                "reference": "GL-500" if attribute_id == 11 else "GL-1L",
            }
        },
    }


def product_html(value: dict[str, Any], *, combinations: bool = True) -> str:
    selector = ""
    if combinations:
        selector = (
            '<select name="group[1]">'
            '<option value="11" selected>500 ml</option><option value="12">1 L</option>'
            "</select>"
        )
    encoded = html.escape(json.dumps(value), quote=True)
    return (
        f'<html><div id="product-details" data-product="{encoded}"></div>{selector}'
        '<a href="/download?id_attachment=9">Safety data sheet</a></html>'
    )


class Transport:
    def __init__(self, documents: dict[str, Any], advertised: tuple[str, ...] = ()) -> None:
        self.documents = documents
        self.advertised = advertised
        self.calls: list[tuple[str, bool, str | None]] = []

    async def allowed(self, url: str) -> bool:
        return True

    async def advertised_sitemaps(self, base_url: str) -> tuple[str, ...]:
        return self.advertised

    async def document(self, url: str, *, rendered=False, accept=None) -> str:
        self.calls.append((url, rendered, accept))
        value = self.documents[url]
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)


def request(*, limit: int | None = None, budget: int | None = None) -> CollectionRequest:
    return CollectionRequest(
        source_id="presta-shop",
        base_url="https://shop.test/",
        refresh_mode=RefreshMode.FULL,
        requested_fields=frozenset(SnapshotField),
        result_limit=limit,
        request_budget=budget,
    )


async def collect(connector: PrestaShopConnector, checkpoint=None, intent=None):
    return [page async for page in connector.collect(intent or request(), checkpoint)]


@pytest.mark.asyncio
async def test_shared_request_budget_exhaustion_preserves_product_resume_cursor() -> None:
    transport = Transport(
        {"https://shop.test/category": '<a href="/product/7-glaze.html">Glaze</a>'}
    )
    budget = RequestBudget(RequestCost(http_requests=1))
    connector = PrestaShopConnector(
        transport,
        PrestaShopOptions(
            category_urls=("https://shop.test/category",),
            use_advertised_sitemaps=False,
            product_pattern=r"/product/",
        ),
        budget=budget,
    )

    [page] = await collect(connector)

    assert page.terminal and not page.enumeration_intact
    assert page.resume_after == {
        "partition": page.partition_key,
        "offset": 0,
        "sequence": 0,
    }
    assert page.diagnostics[0].code == DiagnosticCode.REQUEST_BUDGET_EXHAUSTED
    assert budget.used == RequestCost(http_requests=1)
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_embedded_product_and_combinations_map_to_neutral_variants() -> None:
    first = details()
    first["access_token"] = "embedded-ephemeral-secret"
    second = details(attribute_id=12, price="€18,00", quantity=0)
    combination_url = (
        "https://shop.test/product/7-glaze.html?group%5B1%5D=12&ajax=1&action=refresh&quantity_wanted=1"
    )
    transport = Transport(
        {
            "https://shop.test/category": '<a href="/product/7-glaze.html">Glaze</a>',
            "https://shop.test/product/7-glaze.html": product_html(first),
            combination_url: json.dumps(
                {
                    "product_details": product_html(second, combinations=False),
                    "product_url": "https://shop.test/product/7-glaze.html",
                }
            ),
        }
    )
    connector = PrestaShopConnector(
        transport,
        PrestaShopOptions(
            category_urls=("https://shop.test/category",),
            use_advertised_sitemaps=False,
            product_pattern=r"/product/",
            vat_status="inclusive",
        ),
        clock=lambda: NOW,
    )

    [page] = await collect(connector)

    assert page.terminal and page.enumeration_intact and page.resume_after is None
    snapshot = page.items[0]
    assert [variant.title for variant in snapshot.variants] == ["Size: 500 ml", "Size: 1 L"]
    assert snapshot.variants[0].offers[0].role == "sale"
    assert snapshot.variants[0].offers[1].price.amount == 15
    assert snapshot.variants[0].stock is not None
    assert snapshot.variants[0].stock.quantity_kind == StockQuantityKind.EXACT
    assert snapshot.variants[1].stock is not None and snapshot.variants[1].stock.quantity == 0
    assert snapshot.documents[0].url == "https://shop.test/download?id_attachment=9"
    assert snapshot.published_attributes["Finish"] == "Gloss"
    assert "embedded-ephemeral-secret" not in page.model_dump_json()
    assert snapshot.variants[0].platform_extensions["legacy_raw_record"]["access_token"] == "[redacted]"


@pytest.mark.asyncio
async def test_checkpoint_uses_declared_partition_order_not_lexical_order() -> None:
    transport = Transport(
        {
            "https://shop.test/zeta": '<a href="/product/1.html">one</a>',
            "https://shop.test/alpha": '<a href="/product/2.html">two</a>',
            "https://shop.test/product/2.html": product_html(details(), combinations=False),
        }
    )
    connector = PrestaShopConnector(
        transport,
        PrestaShopOptions(
            category_urls=("https://shop.test/zeta", "https://shop.test/alpha"),
            use_advertised_sitemaps=False,
            product_pattern=r"/product/",
            variant_combinations=False,
        ),
        clock=lambda: NOW,
    )
    checkpoint = ConnectorCheckpoint(
        connector="prestashop",
        connector_version="1",
        source_id="presta-shop",
        lineage="ordered",
        resume_after={"partition": "category:1:4f703219adba", "offset": 0, "sequence": 1},
    )

    [page] = await collect(connector, checkpoint)

    assert page.partition_key == "category:1:4f703219adba"
    assert page.sequence == 1
    assert not any(call[0] == "https://shop.test/product/1.html" for call in transport.calls)


@pytest.mark.asyncio
async def test_product_failure_is_resumable_and_redacts_query_secrets() -> None:
    target = "https://shop.test/product/7.html?access_token=ephemeral"
    category = "https://shop.test/category?access_token=partition-secret"
    transport = Transport(
        {
            category: f'<a href="{target}">seven</a>',
            target: httpx.ReadTimeout("ephemeral"),
        }
    )
    connector = PrestaShopConnector(
        transport,
        PrestaShopOptions(
            category_urls=(category,),
            use_advertised_sitemaps=False,
            product_pattern=r"/product/",
        ),
        clock=lambda: NOW,
    )

    [page] = await collect(connector)

    assert not page.enumeration_intact
    assert page.diagnostics[0].code == DiagnosticCode.ENTITY_FETCH_FAILED
    assert page.resume_after == {
        "partition": "category:0:c0d759344c4d",
        "offset": 0,
        "sequence": 0,
    }
    assert "ephemeral" not in page.model_dump_json()
    assert "partition-secret" not in page.model_dump_json()


@pytest.mark.asyncio
async def test_product_limit_is_strict_and_keeps_resume_cursor() -> None:
    transport = Transport(
        {
            "https://shop.test/category": (
                '<a href="/product/1.html">one</a><a href="/product/2.html">two</a>'
            ),
            "https://shop.test/product/1.html": product_html(details(), combinations=False),
        }
    )
    connector = PrestaShopConnector(
        transport,
        PrestaShopOptions(
            category_urls=("https://shop.test/category",),
            use_advertised_sitemaps=False,
            product_pattern=r"/product/",
            page_limit=1,
            variant_combinations=False,
        ),
        clock=lambda: NOW,
    )

    [page] = await collect(connector)

    assert page.terminal and not page.enumeration_intact
    assert page.resume_after == {
        "partition": "category:0:c0d759344c4d",
        "offset": 1,
        "sequence": 1,
    }
    assert not any(call[0].endswith("/product/2.html") for call in transport.calls)


@pytest.mark.asyncio
async def test_caller_result_limit_uses_non_error_typed_diagnostic() -> None:
    transport = Transport(
        {
            "https://shop.test/category": (
                '<a href="/product/1.html">one</a><a href="/product/2.html">two</a>'
            ),
            "https://shop.test/product/1.html": product_html(details(), combinations=False),
        }
    )
    connector = PrestaShopConnector(
        transport,
        PrestaShopOptions(
            category_urls=("https://shop.test/category",),
            use_advertised_sitemaps=False,
            product_pattern=r"/product/",
            variant_combinations=False,
        ),
        clock=lambda: NOW,
    )

    [page] = await collect(connector, intent=request(limit=1))

    assert page.terminal and not page.enumeration_intact
    assert page.diagnostics[-1].code == DiagnosticCode.RESULT_LIMIT_REACHED
    assert page.resume_after == {
        "partition": "category:0:c0d759344c4d",
        "offset": 1,
        "sequence": 1,
    }


@pytest.mark.asyncio
async def test_request_budget_stops_before_unbudgeted_product_request() -> None:
    transport = Transport(
        {"https://shop.test/category": '<a href="/product/1.html">one</a>'}
    )
    connector = PrestaShopConnector(
        transport,
        PrestaShopOptions(
            category_urls=("https://shop.test/category",),
            use_advertised_sitemaps=False,
            product_pattern=r"/product/",
        ),
        clock=lambda: NOW,
    )

    [page] = await collect(connector, intent=request(budget=1))

    assert page.diagnostics[0].code == DiagnosticCode.ENTITY_FETCH_FAILED
    assert len(transport.calls) == 1
