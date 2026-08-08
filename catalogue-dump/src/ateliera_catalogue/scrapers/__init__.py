"""Per-supplier scrapers for the ceramics catalogue dump.

Each source in sources.json names a scraper here. Platform scrapers cover a
whole storefront family through its public API; the named ones handle a single
supplier whose site needs its own rules.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from .base import Scraper

#: scraper name -> "module:class", imported on demand.
#:
#: The module names are relative to this package. They used to be absolute
#: (`scrapers.shopify`), which resolved only because Python put the directory of
#: the running script on `sys.path` — the coincidence §4.1 of the plan is about.
#: Relative names resolve from an installed distribution instead, so a worker in
#: an image imports these the same way the CLI does.
REGISTRY: dict[str, str] = {
    "shopify": ".shopify:ShopifyScraper",
    "woocommerce": ".woocommerce:WooCommerceScraper",
    "bigcommerce": ".bigcommerce:BigCommerceScraper",
    "prestashop": ".prestashop:PrestaShopScraper",
    # generic json-ld page crawler, for storefronts with no public API.
    # Reads schema.org json-ld only; a microdata-only storefront yields nothing.
    "pagecrawl": ".pagecrawl:PageScraper",
    "sio2": ".prestashop:Sio2Scraper",
    "wix": ".wix:WixScraper",
    "shopware": ".shopware:ShopwareScraper",
    "starweb": ".starweb:StarwebScraper",
    "ceramicolours": ".ceramicolours:CeramicoloursScraper",
    "keramik_kraft": ".keramik_kraft:KeramikKraftScraper",
}


def load(name: str) -> type[Scraper]:
    """Resolve a registry name to its Scraper class."""
    try:
        target = REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown scraper '{name}'; known: {', '.join(sorted(REGISTRY))}") from None
    module_name, class_name = target.split(":")
    return getattr(import_module(module_name, __name__), class_name)


def build(name: str, source_name: str, config: dict[str, Any], fetcher: Any) -> Scraper:
    return load(name)(source_name, config, fetcher)
