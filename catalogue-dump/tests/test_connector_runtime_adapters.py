from __future__ import annotations

import gzip

import httpx
import pytest

from mb_ceramics_catalogue.config.sources import SourceConfig
from mb_ceramics_catalogue.connectors import (
    BigCommerceConnector,
    NitroSellConnector,
    ShopifyConnector,
    ShopwareConnector,
    StarwebConnector,
    SumUpConnector,
    WixConnector,
    WooCommerceConnector,
)
from mb_ceramics_catalogue.ops.connector_adapters import (
    RUNTIME_ADAPTERS,
    ConnectorRuntimePlan,
    WixFetcherTransport,
    runtime_plan,
)
from mb_ceramics_catalogue.pipeline.budget import RequestBudget, RequestCost


class Fetcher:
    async def response(self, url, **kwargs):
        del kwargs
        return httpx.Response(200, content=gzip.compress(b"<urlset/>"), request=httpx.Request("GET", url))


@pytest.mark.parametrize(
    ("scraper", "fields", "connector_type", "partitions"),
    [
        ("shopify", {"collections": ["clay"]}, ShopifyConnector, ("clay",)),
        (
            "woocommerce",
            {"store_categories": ["glazes"], "variation_page_limit": 17},
            WooCommerceConnector,
            ("glazes",),
        ),
        ("bigcommerce", {"category_url": "https://shop.test/token"}, BigCommerceConnector, ("main",)),
        ("wix", {"sitemaps": ["https://shop.test/sitemap.xml"]}, WixConnector, ("main",)),
        (
            "shopware",
            {"category_urls": ["https://shop.test/clay"], "use_advertised_sitemaps": False},
            ShopwareConnector, ("category",),
        ),
        (
            "starweb",
            {"category_urls": ["https://shop.test/clay"], "use_advertised_sitemaps": False},
            StarwebConnector, ("category",),
        ),
        (
            "nitrosell",
            {"category_urls": ["https://shop.test/clay"], "use_advertised_sitemaps": False},
            NitroSellConnector, ("category",),
        ),
        ("sumup", {}, SumUpConnector, ("sitemap",)),
    ],
)
def test_runtime_registry_constructs_every_connector(
    scraper, fields, connector_type, partitions
):
    config = SourceConfig(
        label="Shop", url="https://shop.test/", scraper=scraper, **fields
    )
    plan = runtime_plan(config)
    connector = plan.build(Fetcher(), None)

    assert set(RUNTIME_ADAPTERS) >= {
        "shopify", "woocommerce", "bigcommerce", "wix",
        "shopware", "starweb", "nitrosell", "sumup",
    }
    assert plan.name == scraper
    assert plan.connector_version == connector.version
    assert plan.partitions == partitions
    assert plan.legacy_scraper_adapter == f"{scraper}_connector"
    assert isinstance(connector, connector_type)
    assert plan.options


@pytest.mark.asyncio
async def test_wix_transport_preserves_compressed_sitemap_decoding():
    document = await WixFetcherTransport(Fetcher()).document(
        "https://shop.test/sitemap.xml.gz", accept="application/xml"
    )
    assert document == "<urlset/>"


def test_unknown_runtime_adapter_fails_with_registered_names():
    config = SourceConfig(label="Shop", url="https://shop.test/", scraper="shopify_connector")
    with pytest.raises(ValueError, match=r"shopify.*wix"):
        runtime_plan(config)


@pytest.mark.parametrize("scraper", sorted(RUNTIME_ADAPTERS))
def test_every_registered_projector_returns_a_complete_plan(scraper):
    config = SourceConfig(label="Shop", url="https://shop.test/", scraper=scraper)
    assert isinstance(runtime_plan(config), ConnectorRuntimePlan)


@pytest.mark.parametrize("scraper", sorted(RUNTIME_ADAPTERS))
def test_every_runtime_forwards_the_shared_request_budget(scraper):
    config = SourceConfig(label="Shop", url="https://shop.test/", scraper=scraper)
    budget = RequestBudget(RequestCost(http_requests=10, browser_requests=10))

    connector = runtime_plan(config).build(Fetcher(), budget)

    assert connector._budget.budget is budget
