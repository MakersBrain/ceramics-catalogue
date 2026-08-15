"""Deterministic typed projection of the legacy ``sources.json`` contract.

The legacy model remains the accepted configuration during migration.  This
module gives new orchestration code separated connector/browser/dataset views
without changing what any existing scraper receives.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mb_ceramics_catalogue.datasets.registry import DATASET_NAMES

from .sources import SourceConfig, SourcesFile


class ShopifyConnectorOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["shopify"] = "shopify"
    collections: tuple[str, ...] = ()
    page_limit: int = 200
    inventory_method: Literal["none", "product_json", "product_html"] = "none"
    inventory_section_id: str | None = None
    inventory_prefilter_materials: bool = False


class WooCommerceConnectorOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["woocommerce"] = "woocommerce"
    store_categories: tuple[str, ...] = ()
    page_limit: int = 100
    variation_page_limit: int = 200
    category_page_limit: int = 20
    stock_from_add_to_cart_maximum: bool = False


class BigCommerceConnectorOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["bigcommerce"] = "bigcommerce"
    token_page: str | None = None
    page_limit: int = 200


class WixConnectorOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["wix"] = "wix"
    sitemaps: tuple[str, ...] = ()
    use_advertised_sitemaps: bool = True
    product_pattern: str | None = None
    page_limit: int = 500


class SpecializedPageConnectorOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sitemaps: tuple[str, ...] = ()
    category_urls: tuple[str, ...] = ()
    use_advertised_sitemaps: bool = True
    product_pattern: str | None = None
    pagination_patterns: tuple[str, ...] = ()
    card_links_only: bool = False
    page_limit: int = 500
    category_page_limit: int = 120


class ShopwareConnectorOptions(SpecializedPageConnectorOptions):
    kind: Literal["shopware"] = "shopware"


class SumUpConnectorOptions(SpecializedPageConnectorOptions):
    kind: Literal["sumup"] = "sumup"


class StarwebConnectorOptions(SpecializedPageConnectorOptions):
    kind: Literal["starweb"] = "starweb"


class NitroSellConnectorOptions(SpecializedPageConnectorOptions):
    kind: Literal["nitrosell"] = "nitrosell"


class PrestaShopSourceOptions(SpecializedPageConnectorOptions):
    kind: Literal["prestashop"] = "prestashop"
    variant_combinations: bool = True


class Sio2SourceOptions(SpecializedPageConnectorOptions):
    kind: Literal["sio2"] = "sio2"
    variant_combinations: bool = True


class PageCommerceSourceOptions(SpecializedPageConnectorOptions):
    kind: Literal["pagecommerce"] = "pagecommerce"
    stock_from_quantity_maximum: bool = False


class AxnerSourceOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["axner"] = "axner"
    category_url: str | None = None
    category_page_limit: int = 400
    page_limit: int = 500


class CeramicoloursSourceOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["ceramicolours"] = "ceramicolours"
    category_ids: tuple[str, ...] = ()
    category_page_limit: int = 25
    page_limit: int = 500


class KeramikKraftSourceOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["keramik_kraft"] = "keramik_kraft"
    category_paths: tuple[str, ...] = ()
    category_page_limit: int = 150
    page_limit: int = 500


class LegacyConnectorOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["legacy"] = "legacy"
    scraper: str
    options: dict[str, Any] = Field(default_factory=dict)


ConnectorOptions = Annotated[
    ShopifyConnectorOptions
    | WooCommerceConnectorOptions
    | BigCommerceConnectorOptions
    | WixConnectorOptions
    | ShopwareConnectorOptions
    | SumUpConnectorOptions
    | StarwebConnectorOptions
    | NitroSellConnectorOptions
    | PrestaShopSourceOptions
    | Sio2SourceOptions
    | PageCommerceSourceOptions
    | AxnerSourceOptions
    | CeramicoloursSourceOptions
    | KeramikKraftSourceOptions
    | LegacyConnectorOptions,
    Field(discriminator="kind"),
]


class AutoBrowserOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["auto"] = "auto"
    logical_profile: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")


class CamoufoxBrowserOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["camoufox"] = "camoufox"
    logical_profile: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")


class CdpExtensionProxyBrowserOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["cdp_extension_proxy"] = "cdp_extension_proxy"
    logical_profile: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")


BrowserOptions = Annotated[
    AutoBrowserOptions | CamoufoxBrowserOptions | CdpExtensionProxyBrowserOptions,
    Field(discriminator="kind"),
]


class CrawlOptions(BaseModel):
    """Source-owned collection policy, separate from connector parsing."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    delay: float | None = None
    timeout_seconds: float | None = None
    product_concurrency: int | None = None
    ignore_robots: bool = False
    obey_robots: bool | None = None
    render: bool | None = None
    proxy_eligible: bool = False
    proxy_policy: Literal["never", "fallback", "always"] = "never"
    proxy_profile: str | None = None
    proxy_country: str | None = None
    proxy_session_minutes: int = 30
    proxy_max_megabytes: int = 25
    proxy_pilot: bool = False


class CeramicsDatasetOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["ceramics"] = "ceramics"
    dataset: Literal["ceramics.catalogue_item.v2", "ceramics.catalogue_identity.v2"]
    scope: Literal["materials", "all"]
    enrichments: tuple[str, ...] = ()
    brand: str | None = None
    is_manufacturer: bool = False
    material_categories: tuple[str, ...] = ()
    excluded_categories: tuple[str, ...] = ()
    currency: str | None = None
    vat_status: Literal["inclusive", "exclusive", "unknown"] | None = None
    vat_rate: float | None = None


class CommerceDatasetOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["commerce"] = "commerce"
    dataset: Literal[
        "commerce.price_observation.v1",
        "commerce.stock_observation.v1",
        "commerce.document.v1",
    ]


DatasetOptions = Annotated[
    CeramicsDatasetOptions | CommerceDatasetOptions, Field(discriminator="kind")
]


class TypedSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: str
    label: str
    url: str
    country: str | None = None
    note: str | None = None
    connector: ConnectorOptions
    browser: BrowserOptions
    crawl: CrawlOptions
    datasets: tuple[DatasetOptions, ...]
    available_datasets: tuple[str, ...]


class ProjectionInspection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source: TypedSourceConfig
    ambiguous_fields: tuple[str, ...] = ()
    unused_fields: tuple[str, ...] = ()


_IDENTITY_FIELDS = {"label", "url", "scraper", "country", "note"}
_POLICY_FIELDS = {
    "delay", "ignore_robots", "obey_robots", "render", "product_concurrency",
    "timeout_seconds", "proxy_eligible", "proxy_policy", "proxy_profile",
    "proxy_country", "proxy_session_minutes", "proxy_max_megabytes", "proxy_pilot",
}
_DATASET_FIELDS = {
    "scope", "brand", "is_manufacturer", "enrichments", "material_categories",
    "excluded_categories", "identity_only", "vat_status", "vat_rate", "currency",
}
_DISCOVERY_FIELDS = {
    "sitemaps", "use_advertised_sitemaps", "product_pattern", "pagination_patterns",
    "page_limit", "category_url", "category_urls", "category_ids", "category_paths",
    "category_page_limit", "collections", "store_categories", "card_links_only",
    "enrich_product_pages", "variation_page_limit", "variant_combinations",
    "stock_from_add_to_cart_maximum", "stock_from_quantity_maximum",
    "inventory_product_json", "inventory_product_html", "inventory_section_id",
    "inventory_prefilter_materials",
}


def project_legacy_source(source_id: str, source: SourceConfig) -> ProjectionInspection:
    """Project one validated legacy source; output ordering is stable."""
    explicit = source.model_fields_set
    ambiguous: list[str] = []
    if source.inventory_product_json and source.inventory_product_html:
        ambiguous.append("inventory_product_json|inventory_product_html")

    if source.scraper == "shopify":
        connector: ConnectorOptions = ShopifyConnectorOptions(
            collections=tuple(source.collections or ()),
            page_limit=source.page_limit or 200,
            inventory_method=(
                "product_html" if source.inventory_product_html else
                "product_json" if source.inventory_product_json else "none"
            ),
            inventory_section_id=source.inventory_section_id,
            inventory_prefilter_materials=bool(source.inventory_prefilter_materials),
        )
        supported = {
            "collections", "page_limit", "inventory_product_json", "inventory_product_html",
            "inventory_section_id", "inventory_prefilter_materials",
        }
        unused = sorted(
            explicit - _IDENTITY_FIELDS - _POLICY_FIELDS - _DATASET_FIELDS - supported
        )
    elif source.scraper == "woocommerce":
        supported = {
            "store_categories", "page_limit", "variation_page_limit",
            "category_page_limit", "stock_from_add_to_cart_maximum",
        }
        connector = WooCommerceConnectorOptions(
            store_categories=tuple(source.store_categories or ()),
            page_limit=source.page_limit or 100,
            variation_page_limit=source.variation_page_limit or 200,
            category_page_limit=source.category_page_limit or 20,
            stock_from_add_to_cart_maximum=bool(source.stock_from_add_to_cart_maximum),
        )
        unused = sorted(explicit - _IDENTITY_FIELDS - _POLICY_FIELDS - _DATASET_FIELDS - supported)
    elif source.scraper == "bigcommerce":
        supported = {"category_url", "page_limit"}
        connector = BigCommerceConnectorOptions(
            token_page=source.category_url, page_limit=source.page_limit or 200
        )
        unused = sorted(explicit - _IDENTITY_FIELDS - _POLICY_FIELDS - _DATASET_FIELDS - supported)
    elif source.scraper == "wix":
        supported = {"sitemaps", "use_advertised_sitemaps", "product_pattern", "page_limit"}
        connector = WixConnectorOptions(
            sitemaps=tuple(source.sitemaps or ()),
            use_advertised_sitemaps=(
                True if source.use_advertised_sitemaps is None
                else source.use_advertised_sitemaps
            ),
            product_pattern=source.product_pattern,
            page_limit=source.page_limit or 500,
        )
        unused = sorted(explicit - _IDENTITY_FIELDS - _POLICY_FIELDS - _DATASET_FIELDS - supported)
    elif source.scraper in {"shopware", "sumup", "starweb", "nitrosell"}:
        supported = {
            "sitemaps", "category_urls", "use_advertised_sitemaps", "product_pattern",
            "pagination_patterns", "card_links_only", "page_limit", "category_page_limit",
        }
        option_types = {
            "shopware": ShopwareConnectorOptions,
            "sumup": SumUpConnectorOptions,
            "starweb": StarwebConnectorOptions,
            "nitrosell": NitroSellConnectorOptions,
        }
        connector = option_types[source.scraper](
            sitemaps=tuple(source.sitemaps or ()),
            category_urls=tuple(source.category_urls or ()),
            use_advertised_sitemaps=(True if source.use_advertised_sitemaps is None
                                    else source.use_advertised_sitemaps),
            product_pattern=source.product_pattern,
            pagination_patterns=tuple(source.pagination_patterns or ()),
            card_links_only=bool(source.card_links_only),
            page_limit=source.page_limit or 500,
            category_page_limit=source.category_page_limit or 120,
        )
        unused = sorted(explicit - _IDENTITY_FIELDS - _POLICY_FIELDS - _DATASET_FIELDS - supported)
    elif source.scraper in {"prestashop", "sio2", "pagecrawl"}:
        supported = {
            "sitemaps", "category_urls", "use_advertised_sitemaps", "product_pattern",
            "pagination_patterns", "card_links_only", "page_limit", "category_page_limit",
            "variant_combinations", "stock_from_quantity_maximum",
            "enrich_product_pages",
        }
        common = {
            "sitemaps": tuple(source.sitemaps or ()),
            "category_urls": tuple(source.category_urls or ()),
            "use_advertised_sitemaps": (
                True if source.use_advertised_sitemaps is None else source.use_advertised_sitemaps
            ),
            "product_pattern": source.product_pattern,
            "pagination_patterns": tuple(source.pagination_patterns or ()),
            "card_links_only": bool(source.card_links_only),
            "page_limit": source.page_limit or 500,
            "category_page_limit": source.category_page_limit or 120,
        }
        connector = (
            PageCommerceSourceOptions.model_validate({
                **common, "stock_from_quantity_maximum": bool(source.stock_from_quantity_maximum)
            })
            if source.scraper == "pagecrawl"
            else (Sio2SourceOptions if source.scraper == "sio2" else PrestaShopSourceOptions)
            .model_validate({
                **common,
                "variant_combinations": (True if source.variant_combinations is None
                                         else source.variant_combinations),
            })
        )
        unused = sorted(explicit - _IDENTITY_FIELDS - _POLICY_FIELDS - _DATASET_FIELDS - supported)
    elif source.scraper in {"axner", "ceramicolours", "keramik_kraft"}:
        if source.scraper == "axner":
            connector = AxnerSourceOptions(
                category_url=source.category_url,
                category_page_limit=source.category_page_limit or 400,
                page_limit=source.page_limit or 500,
            )
            supported = {"category_url", "category_page_limit", "page_limit"}
        elif source.scraper == "ceramicolours":
            connector = CeramicoloursSourceOptions(
                category_ids=tuple(str(value) for value in (source.category_ids or ())),
                category_page_limit=source.category_page_limit or 25,
                page_limit=source.page_limit or 500,
            )
            supported = {"category_ids", "category_page_limit", "page_limit"}
        else:
            connector = KeramikKraftSourceOptions(
                category_paths=tuple(source.category_paths or ()),
                category_page_limit=source.category_page_limit or 150,
                page_limit=source.page_limit or 500,
            )
            supported = {"category_paths", "category_page_limit", "page_limit"}
        unused = sorted(explicit - _IDENTITY_FIELDS - _POLICY_FIELDS - _DATASET_FIELDS - supported)
    else:
        legacy = source.as_scraper_config()
        options = {key: legacy[key] for key in sorted(explicit & _DISCOVERY_FIELDS) if key in legacy}
        connector = LegacyConnectorOptions(scraper=source.scraper, options=options)
        unused = sorted(
            explicit - _IDENTITY_FIELDS - _POLICY_FIELDS - _DATASET_FIELDS - _DISCOVERY_FIELDS
        )

    browser: BrowserOptions = (
        CamoufoxBrowserOptions() if source.render is True else AutoBrowserOptions()
    )
    crawl = CrawlOptions(
        delay=source.delay,
        timeout_seconds=source.timeout_seconds,
        product_concurrency=source.product_concurrency,
        ignore_robots=source.ignore_robots,
        obey_robots=source.obey_robots,
        render=source.render,
        proxy_eligible=source.proxy_eligible,
        proxy_policy=source.proxy_policy,
        proxy_profile=source.proxy_profile,
        proxy_country=source.proxy_country,
        proxy_session_minutes=source.proxy_session_minutes,
        proxy_max_megabytes=source.proxy_max_megabytes,
        proxy_pilot=source.proxy_pilot,
    )
    ceramics_name: Literal[
        "ceramics.catalogue_item.v2", "ceramics.catalogue_identity.v2"
    ] = (
        "ceramics.catalogue_identity.v2" if source.identity_only
        else "ceramics.catalogue_item.v2"
    )
    datasets: tuple[DatasetOptions, ...] = (
        CeramicsDatasetOptions(
            dataset=ceramics_name,
            scope=source.scope,
            enrichments=tuple(source.enrichments or ()),
            brand=source.brand,
            is_manufacturer=source.is_manufacturer,
            material_categories=tuple(source.material_categories or ()),
            excluded_categories=tuple(source.excluded_categories or ()),
            currency=source.currency,
            vat_status=source.vat_status,
            vat_rate=source.vat_rate,
        ),
    )
    assert {item.dataset for item in datasets} <= DATASET_NAMES
    return ProjectionInspection(
        source=TypedSourceConfig(
            source_id=source_id, label=source.label, url=source.url, country=source.country,
            note=source.note, connector=connector, browser=browser, crawl=crawl,
            datasets=datasets, available_datasets=tuple(sorted(DATASET_NAMES)),
        ),
        ambiguous_fields=tuple(sorted(ambiguous)), unused_fields=tuple(unused),
    )


def inspect_sources(sources: SourcesFile) -> tuple[ProjectionInspection, ...]:
    return tuple(project_legacy_source(name, sources[name]) for name in sorted(sources))
