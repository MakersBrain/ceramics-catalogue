"""Axner Pottery Supply, an AmeriCommerce storefront with no machine-readable feed.

Axner publishes neither JSON-LD nor microdata nor an XML sitemap, so both halves
of the job are site-specific. Discovery starts from `/sitemap.aspx`, an HTML
index of every department, and walks each department's `?page=N` pagination.
Product and category URLs are both hyphenated `.aspx` slugs and cannot be told
apart by shape, so a product link is recognised by the tile class the listing
grid wraps it in rather than by its address.

Each product page then carries the useful fields in `data-item` blocks the
platform writes, which is stable markup even though it is not a standard.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from . import domain, jsonld
from . import record as record_module
from .pagecrawl import PageScraper, canonical

#: `<h5 class="category-page product-list-link"><a href="/spectrum-501-...aspx">`
LISTING_LINK = re.compile(r'class="[^"]*product-list-link[^"]*"[^>]*>\s*<a[^>]+href="([^"]+)"', re.I)
NEXT_PAGE = re.compile(r'href="([^"]*\?page=\d+)"', re.I)
TITLE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)
PRICE = re.compile(r'class="[^"]*product-list-cost-value[^"]*"[^>]*>\s*\$?\s*([\d,]+\.\d{2})', re.I)
MANUFACTURER = re.compile(r'class="prod-detail-man-name-value"[^>]*>(.*?)</span>', re.I | re.S)
DESCRIPTION = re.compile(r'class="prod-detail-desc"[^>]*>(.*?)</div>', re.I | re.S)
IMAGE = re.compile(r'src="(/ProductImages/[^"]+)"', re.I)
OPTIONS = re.compile(r"options[- ]available", re.I)
TAGS = re.compile(r"<[^>]+>")

#: `<span class="prod-detail-part-label">Axner Number:</span><span class="...-value">A521310</span>`
DETAIL_PART = re.compile(
    r'class="prod-detail-part-label"[^>]*>(.*?)</span>\s*<span[^>]*class="prod-detail-part-value"[^>]*>(.*?)</span>',
    re.I | re.S,
)


def _text(fragment: str) -> str:
    return domain.clean(html_lib.unescape(TAGS.sub(" ", fragment)))


class AxnerScraper(PageScraper):
    method = "html"

    async def discover(self, limit: int | None = None) -> list[str]:
        index = self.config.get("category_url") or urljoin(self.base_url, "/sitemap.aspx")
        document = await self.load(index)
        if document is None:
            self.note("the HTML sitemap could not be read; no products discovered")
            return []
        origin = urlparse(self.base_url).netloc
        departments = [
            canonical(urljoin(index, href))
            for href in dict.fromkeys(re.findall(r'href="(/[A-Za-z0-9\-]+\.aspx)"', document))
        ]
        departments = [url for url in departments if urlparse(url).netloc == origin]
        self.note(f"{len(departments)} departments from {index}")

        products: list[str] = []
        seen_pages: set[str] = set()
        page_limit = self.config.get("category_page_limit", 400)
        queue = list(departments)
        while queue and len(seen_pages) < page_limit:
            if limit is not None and len(products) >= limit:
                break
            url = queue.pop(0)
            if url in seen_pages:
                continue
            seen_pages.add(url)
            listing = await self.load(url)
            if listing is None:
                continue
            for href in LISTING_LINK.findall(listing):
                candidate = canonical(urljoin(url, html_lib.unescape(href)))
                if urlparse(candidate).netloc == origin and candidate not in products:
                    products.append(candidate)
            for href in NEXT_PAGE.findall(listing):
                page = canonical(urljoin(url, html_lib.unescape(href)))
                if page not in seen_pages and page not in queue:
                    queue.append(page)
        return products

    def parse(self, document: str, url: str) -> list[tuple[dict[str, Any], bool | None]]:
        title = TITLE.search(document)
        name = _text(title.group(1)) if title else (jsonld.meta(document, "og:title") or "")
        if not name:
            return []
        price_match = PRICE.search(document)
        if not price_match:
            # Axner hides the price on items it only sells on application; a row
            # without one is not an offer and is dropped rather than recorded.
            return []
        price, currency = record_module.parse_price(price_match.group(1))
        if price is None:
            return []

        details = {_text(label).rstrip(":"): _text(value) for label, value in DETAIL_PART.findall(document)}
        reference = details.get("Axner Number") or None
        manufacturer = MANUFACTURER.search(document)
        brand = _text(manufacturer.group(1)) if manufacturer else self.config.get("brand")
        description = DESCRIPTION.search(document)
        images = [
            urljoin(url, candidate) for candidate in dict.fromkeys(IMAGE.findall(document))
            if "thumb" not in candidate.rsplit("/", 1)[-1].lower()
        ]
        attributes = {key: value for key, value in details.items() if key != "Axner Number"}

        row = record_module.build(
            source=self.name,
            product_url=canonical(url),
            name=name,
            brand=brand,
            manufacturer_sku=domain.manufacturer_code(brand, name, reference),
            supplier_reference=reference,
            description=_text(description.group(1)) if description else None,
            category_path=None,
            image_url=images[0] if images else None,
            all_image_urls=images or None,
            price=price,
            currency=currency or self.config.get("currency"),
            price_text=f"{price_match.group(1)} {currency or self.config.get('currency') or ''}".strip(),
            vat=self.config.get("vat_status"),
            vat_rate=self.config.get("vat_rate"),
            technical_attributes=attributes or None,
            documents=domain.documents(jsonld.pdf_links(document, url), url) or None,
            extraction_method=self.method,
            source_detail_level="product_page",
            raw={"details": details, "options_available": bool(OPTIONS.search(document))},
        )
        return [(row, self.category_allows(name))]
