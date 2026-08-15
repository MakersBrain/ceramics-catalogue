from __future__ import annotations

import json
from collections import Counter
from typing import Any

import pytest

from mb_ceramics_catalogue import scrapers
from mb_ceramics_catalogue.scrapers.prestashop_connector import PrestaShopConnectorScraper

from .test_prestashop_connector import details, product_html


class Limiter:
    def join_group(self, url: str, group: str) -> None:
        pass

    def set_delay(self, url: str, delay: float) -> None:
        pass


class Stats:
    def __init__(self) -> None:
        self.outcomes: Counter[str] = Counter()


class Fetcher:
    browser_policy = "auto"
    proxy_fallback = None

    def __init__(self, documents: dict[str, str]) -> None:
        self.documents = documents
        self.limiter = Limiter()
        self.stats = Stats()
        self.calls: list[str] = []

    async def robots(self, url: str):
        class Robots:
            def can_fetch(self, agent, target):
                return True

            def crawl_delay(self, agent):
                return None

        return Robots(), []

    async def may_fetch(self, url: str, ignore_robots=False, obey_robots=False) -> bool:
        return True

    async def text(self, url: str, **kwargs: Any) -> str:
        self.calls.append(url)
        return self.documents[url]

    async def render(self, url: str, **kwargs: Any) -> str:
        raise AssertionError("browser should not be used")


def config(kind: str) -> dict[str, Any]:
    return {
        "url": "https://shop.test/",
        "scraper": kind,
        "scope": "all",
        "vat_status": "inclusive",
        "category_urls": ["https://shop.test/category"],
        "product_pattern": r"/product/",
        "variant_combinations": True,
        "enrichments": [],
    }


def stable(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "fetched_at": "<volatile>"}


@pytest.mark.asyncio
async def test_prestashop_canary_is_explicit_and_matches_legacy_variants() -> None:
    first = details()
    second = details(attribute_id=12, price="€18,00", quantity=0)
    combination_url = (
        "https://shop.test/product/7-glaze.html?group%5B1%5D=12&ajax=1&action=refresh&quantity_wanted=1"
    )
    documents = {
        "https://shop.test/category": '<a href="/product/7-glaze.html">Glaze</a>',
        "https://shop.test/product/7-glaze.html": product_html(first),
        combination_url: json.dumps(
            {
                "product_details": product_html(second, combinations=False),
                "product_url": "https://shop.test/product/7-glaze.html",
            }
        ),
    }
    legacy = scrapers.build("prestashop", "shop", config("prestashop"), Fetcher(documents))
    canary = scrapers.build(
        "prestashop_connector",
        "shop",
        config("prestashop_connector"),
        Fetcher(documents),
    )

    legacy_result = await legacy.scrape()
    canary_result = await canary.scrape()

    assert scrapers.load("prestashop").__name__ == "PrestaShopScraper"
    assert scrapers.load("prestashop_connector").__name__ == "PrestaShopConnectorScraper"
    assert len(legacy_result.records) == len(canary_result.records) == 2
    assert [stable(row) for row in canary_result.records] == [
        stable(row) for row in legacy_result.records
    ]
    assert canary_result.discovered == legacy_result.discovered == 1
    assert canary_result.requests == legacy_result.requests == 3


@pytest.mark.asyncio
async def test_prestashop_canary_cancellation_marks_truncated() -> None:
    fetcher = Fetcher({})
    canary = scrapers.build(
        "prestashop_connector",
        "shop",
        config("prestashop_connector"),
        fetcher,
    )
    assert isinstance(canary, PrestaShopConnectorScraper)
    canary.cancel()

    result = await canary.scrape()

    assert result.truncated


@pytest.mark.asyncio
async def test_sio2_canary_applies_typed_policy_with_legacy_parity() -> None:
    value = details()
    value["manufacturer_name"] = "SIO-2"
    value["link"] = "https://shop.test/gb/66-low-fire-ceramic-clays/7-glaze.html"
    documents = {
        "https://shop.test/gb/66-low-fire-ceramic-clays": (
            '<a href="/gb/66-low-fire-ceramic-clays/7-glaze.html">Clay</a>'
        ),
        value["link"]: product_html(value, combinations=False),
    }
    base_config = {
        **config("sio2"),
        "category_urls": ["https://shop.test/gb/66-low-fire-ceramic-clays"],
        "product_pattern": r"/gb/66-low-fire-ceramic-clays/.+\.html$",
        "variant_combinations": False,
    }
    legacy = scrapers.build("sio2", "sio", base_config, Fetcher(documents))
    canary = scrapers.build(
        "sio2_connector",
        "sio",
        {**base_config, "scraper": "sio2_connector"},
        Fetcher(documents),
    )

    legacy_result = await legacy.scrape()
    canary_result = await canary.scrape()

    assert scrapers.load("sio2").__name__ == "Sio2Scraper"
    assert scrapers.load("sio2_connector").__name__ == "Sio2ConnectorScraper"
    assert [stable(row) for row in canary_result.records] == [
        stable(row) for row in legacy_result.records
    ]
