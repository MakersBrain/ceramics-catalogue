"""NitroSell storefronts, read from their OpenGraph product meta.

NitroSell pages do carry a `schema.org/Product` scope, but it holds the name and
nothing else — price, SKU and availability all sit outside it, so the generic
microdata path builds a row with no price and `record.is_valid` drops it. The
whole product is however published as OpenGraph: `product:price:amount`,
`product:price:currency`, `og:availability`, `og:upc` and `og:brand` are all
present and machine-written by the platform, which makes them a better source
than the rendered body.

Everything else — the strike-through list price, the long description and the
breadcrumb — is read from the page, because the meta tags truncate the
description and say nothing about a sale price.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any
from urllib.parse import urljoin

from . import domain, jsonld
from . import record as record_module
from .pagecrawl import PageScraper, canonical

#: `<strong class="priceCurrent">$12.34</strong>` and its struck-through sibling.
CURRENT_PRICE = re.compile(r'class="priceCurrent"[^>]*>([^<]+)<', re.I)
LIST_PRICE = re.compile(r'class="text-pricestrike"[^>]*>([^<]+)<', re.I)
#: `<p class="text-product-desc">Item #: EHHF1210B3K0</p>`
ITEM_NUMBER = re.compile(r"Item\s*#:\s*([^<\s]+)", re.I)
BREADCRUMB = re.compile(r'<ol class="breadcrumb">(.*?)</ol>', re.I | re.S)
CRUMB_LINK = re.compile(r"<li[^>]*>\s*(?:<a[^>]*>)?(.*?)(?:</a>)?\s*</li>", re.I | re.S)
DESCRIPTION = re.compile(r'<div[^>]*class="[^"]*product-description[^"]*"[^>]*>(.*?)</div>', re.I | re.S)
PRODUCT_IMAGE = re.compile(r'https://cdn\.powered-by-nitrosell\.com/product_images/[^"\'\s]+', re.I)
TAGS = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    return domain.clean(html_lib.unescape(TAGS.sub(" ", fragment)))


class NitroSellScraper(PageScraper):
    """One product per page, built from the platform's own meta tags."""

    method = "opengraph"

    def parse(self, document: str, url: str) -> list[tuple[dict[str, Any], bool | None]]:
        name = jsonld.meta(document, "og:title") or self._microdata_name(document)
        if not name:
            return []

        price, currency = record_module.parse_price(jsonld.meta(document, "product:price:amount"))
        currency = jsonld.meta(document, "product:price:currency") or currency or self.config.get("currency")
        if price is None:
            # A page that publishes no price amount still shows one in the body
            # when the item is orderable; a call-for-price item shows none, and
            # dropping it here is correct.
            price, currency = record_module.parse_price(self._first(CURRENT_PRICE, document))
        if price is None:
            return []
        list_price, _ = record_module.parse_price(self._first(LIST_PRICE, document))

        categories = self._breadcrumb(document)
        images = self._images(document, url)
        reference = jsonld.meta(document, "og:upc") or self._first(ITEM_NUMBER, document)
        brand = jsonld.meta(document, "og:brand") or self.config.get("brand")

        row = record_module.build(
            source=self.name,
            product_url=canonical(url),
            name=name,
            brand=brand,
            manufacturer_sku=domain.manufacturer_code(brand, name, reference),
            supplier_reference=reference or None,
            description=self._description(document),
            category_path=categories or None,
            image_url=images[0] if images else None,
            all_image_urls=images or None,
            price=price,
            currency=currency,
            price_text=f"{jsonld.meta(document, 'product:price:amount') or price} {currency or ''}".strip(),
            list_price=list_price if list_price and list_price != price else None,
            vat=self.config.get("vat_status"),
            vat_rate=self.config.get("vat_rate"),
            availability=jsonld.availability(jsonld.meta(document, "og:availability")),
            technical_attributes=jsonld.specification_table(document) or None,
            documents=domain.documents(jsonld.pdf_links(document, url), url) or None,
            extraction_method=self.method,
            source_detail_level="product_page",
            raw={"og": {
                key: jsonld.meta(document, key)
                for key in ("og:title", "og:brand", "og:upc", "og:availability",
                            "product:price:amount", "product:price:currency")
            }},
        )
        return [(row, self.category_allows(" ".join(categories), name))]

    @staticmethod
    def _first(pattern: re.Pattern[str], document: str) -> str:
        match = pattern.search(document)
        return html_lib.unescape(match.group(1)).strip() if match else ""

    @staticmethod
    def _microdata_name(document: str) -> str:
        match = re.search(r'<h1[^>]*itemprop="name"[^>]*>(.*?)</h1>', document, re.I | re.S)
        return _text(match.group(1)) if match else ""

    @staticmethod
    def _breadcrumb(document: str) -> list[str]:
        block = BREADCRUMB.search(document)
        if not block:
            return []
        crumbs = [_text(crumb) for crumb in CRUMB_LINK.findall(block.group(1))]
        # "Home" is navigation, and the last crumb repeats the product title.
        return [crumb for crumb in crumbs[1:-1] if crumb]

    def _description(self, document: str) -> str | None:
        body = DESCRIPTION.search(document)
        if body:
            text = _text(body.group(1))
            if text:
                return text
        # og:description is truncated by the platform, so it is the fallback.
        return jsonld.meta(document, "og:description")

    @staticmethod
    def _images(document: str, url: str) -> list[str]:
        found: list[str] = []
        primary = jsonld.meta(document, "og:image")
        if primary:
            found.append(urljoin(url, primary))
        for candidate in PRODUCT_IMAGE.findall(document):
            # Thumbnails are the same asset at a smaller size, under a `thumb`
            # prefixed basename; keeping both would double every row's images.
            if "/thumb" in candidate or candidate.rsplit("/", 1)[-1].startswith("thumb"):
                continue
            if candidate not in found:
                found.append(candidate)
        return found
