"""Shared crawler for suppliers that publish no usable product API.

Product URLs are discovered from a sitemap where one exists, otherwise by
walking the configured category pages and their pagination. Each product page is
then read for JSON-LD, its specification table and its linked documents. The
browser is used only when the server response carries no usable product data.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import re
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx

from . import domain, jsonld, microdata
from . import record as record_module
from .base import Blocked, BrowserUnavailable, Scraper


def canonical(url: str) -> str:
    """Drop fragments and the sorting/paging noise that duplicates a page."""
    parsed = urlparse(url)
    query = "" if re.search(r"(?:^|&)(?:order|tag|id_currency|search_query|back|q|sort)=", parsed.query) else parsed.query
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, query, ""))


class PageScraper(Scraper):
    """Discover product pages, then read each one."""

    method = "jsonld"
    #: Selector waited for when a page has to be rendered.
    render_wait_for: str | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.product_pattern = re.compile(self.config["product_pattern"]) if self.config.get("product_pattern") else None
        # Three states, not two. `render: true` renders every page; `render`
        # unset leaves the fallback available for a page that parses to
        # nothing; `render: false` declines it outright, which is the setting
        # for a source measured to gain nothing from rendering — it otherwise
        # sends the whole job to the browser worker on one empty page.
        self.always_render = self.config.get("render") is True
        self.never_render = self.config.get("render") is False

    # -- discovery --------------------------------------------------------

    def is_product_url(self, url: str) -> bool:
        if self.product_pattern is None:
            return True
        parsed = urlparse(url)
        return bool(self.product_pattern.search(parsed.path) or self.product_pattern.search(url))

    async def discover(self, limit: int | None = None) -> list[str]:
        urls = await self.discover_from_sitemaps()
        if not urls and not self.config.get("category_urls") and self.config.get("sitemaps"):
            # A source that names its sitemaps and got nothing from them has not
            # found an empty shop; it has failed to look.
            self.enumeration_failed(self.base_url, "configured sitemaps yielded no product URLs")
        if urls:
            self.note(f"{len(urls)} product URLs from the sitemap")
            return urls
        urls = await self.discover_from_categories(limit)
        self.note(f"{len(urls)} product URLs from category pages")
        return urls

    async def discover_from_sitemaps(self) -> list[str]:
        configured = self.config.get("sitemaps") or []
        if not configured and self.config.get("use_advertised_sitemaps", True):
            _, advertised = await self.fetcher.robots(self.base_url)
            configured = advertised
        if not configured:
            return []
        found = await self.sitemap_urls(configured)
        origin = urlparse(self.base_url).netloc
        return [
            canonical(url) for url in found
            if urlparse(url).netloc == origin and self.is_product_url(url)
        ]

    async def discover_from_categories(self, limit: int | None = None) -> list[str]:
        """Walk category pages and their pagination, collecting product links.

        Stops as soon as `limit` products are in hand, so a small sample does not
        pay for a full category walk on a deliberately slow-rated source.
        """
        queue = list(self.config.get("category_urls") or [self.base_url])
        seen: set[str] = set()
        products: list[str] = []
        page_limit = self.config.get("category_page_limit", 120)
        while queue and len(seen) < page_limit and not (limit is not None and len(products) >= limit):
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            document = await self.load(url)
            if document is None:
                continue
            for link in self.links(document, url):
                if self.is_product_url(link):
                    if link not in products:
                        products.append(link)
                elif self.is_pagination(link) and link not in seen and link not in queue:
                    queue.append(link)
        return products

    def links(self, document: str, page_url: str) -> list[str]:
        origin = urlparse(page_url).netloc
        scope = document
        if self.config.get("card_links_only"):
            cards = re.findall(
                r'<(?:article|li|div)[^>]*class=["\'][^"\']*(?:product-miniature|product-item|product-card|productbox)[^"\']*["\'][\s\S]*?</(?:article|li|div)>',
                document, re.I,
            )
            pagination = re.findall(r'<a[^>]+(?:rel=["\']next["\']|class=["\'][^"\']*(?:next|pagination)[^"\']*["\'])[^>]*>', document, re.I)
            scope = "".join([*cards, *pagination]) or document
        found = []
        for match in re.finditer(r'href=["\']([^"\']+)["\']', scope, re.I):
            candidate = canonical(urljoin(page_url, html_lib.unescape(match.group(1))))
            if urlparse(candidate).netloc == origin:
                found.append(candidate)
        return list(dict.fromkeys(found))

    def is_pagination(self, url: str) -> bool:
        patterns = self.config.get("pagination_patterns")
        if patterns:
            return any(re.search(pattern, url) for pattern in patterns)
        return bool(re.search(r"[?&](?:p|page|start)=\d+|/page/\d+", url))

    # -- fetching ---------------------------------------------------------

    async def load(self, url: str, render: bool | None = None) -> str | None:
        """Fetch a page, falling back to the browser when the server refuses."""
        if not await self.fetcher.may_fetch(url, self.ignore_robots, self.obey_robots):
            self.fail(url, "robots.txt disallows this URL")
            return None
        if self.never_render and render:
            # Asked to render a source that has declined it. Not an error and
            # not a browser request: the page simply has nothing more to give.
            return None
        if render or (render is None and self.always_render):
            try:
                document = await self.fetcher.render(url, wait_for=self.render_wait_for)
                self.result.rendered_pages += 1
                return document
            except BrowserUnavailable:
                # This process cannot start a browser at all, which is true of
                # every remaining page too. It has to reach the worker, which
                # requeues the job for one that has a browser; swallowing it
                # here turns a routing decision into 77 lost pages.
                raise
            except Exception as error:  # noqa: BLE001 - browser errors vary
                self.fail(url, error)
                return None
        try:
            document = await self.fetcher.text(url, browser_user_agent=True)
            self.result.requests += 1
            return document
        except (httpx.HTTPError, Blocked, UnicodeError) as error:
            if self.never_render or self.fetcher.browser_policy == "never":
                self.fail(url, error)
                return None
            try:
                document = await self.fetcher.render(url, wait_for=self.render_wait_for)
                self.result.rendered_pages += 1
                return document
            except BrowserUnavailable:
                raise
            except Exception as browser_error:  # noqa: BLE001
                self.fail(url, f"{error} / browser: {browser_error}")
                return None

    async def scrape(self, limit: int | None = None) -> Any:
        urls = await self.discover(limit)
        self.result.discovered = len(urls)
        page_limit = limit if limit is not None else self.config.get("page_limit", 500)
        selected = urls[:page_limit]
        # `or`, not `=`. Discovery may already have found the listing
        # incomplete, and a page limit that happens not to bite is not evidence
        # that it was complete after all.
        self.result.truncated = self.result.truncated or len(urls) > len(selected)
        concurrency = int(self.config.get("product_concurrency", 4))
        semaphore = asyncio.Semaphore(concurrency)

        async def handle(url: str) -> list[tuple[dict[str, Any], bool | None]]:
            async with semaphore:
                # One product page, already listed. A failure here costs that
                # row and says nothing about whether the listing was complete.
                with self.extracting():
                    document = await self.load(url)
                    if document is None:
                        return []
                    try:
                        rows = await self.parse_page(document, url)
                    except Exception as error:  # noqa: BLE001 - one bad page must not stop a source
                        self.fail(url, error)
                        return []
                    if (
                        not rows
                        and not self.always_render
                        and not self.never_render
                        and self.fetcher.browser_policy != "never"
                    ):
                        rendered = await self.load(url, render=True)
                        if rendered:
                            try:
                                rows = await self.parse_page(rendered, url)
                            except Exception as error:  # noqa: BLE001
                                self.fail(url, error)
                                return []
                    return rows

        # Gathered concurrently, added in the order the pages were listed.
        # Appending as each task finishes made the dump's record order a
        # function of how fast each page answered, so two runs over identical
        # data produced different files — which is a real difference for any
        # diff of two dumps, and made the golden digest flaky under load.
        for rows in await asyncio.gather(*(handle(url) for url in selected)):
            for row, category_match in rows:
                self.add(row, category_match)
        return self.result

    # -- parsing ----------------------------------------------------------

    async def parse_page(self, document: str, url: str) -> list[tuple[dict[str, Any], bool | None]]:
        """Async hook for sources that must interact with the page to read it."""
        return self.parse(document, url)

    def parse(self, document: str, url: str) -> list[tuple[dict[str, Any], bool | None]]:
        """Read one product page. Subclasses add platform-specific detail."""
        rows = []
        for item in jsonld.products(document):
            row = self.from_jsonld(item, document, url)
            if row is not None:
                rows.append(row)
        if rows:
            return rows
        # No JSON-LD: some storefronts only mark the product up as microdata,
        # which reads back in the same shape.
        for item in microdata.products(document):
            row = self.from_jsonld(item, document, url, method="microdata")
            if row is not None:
                rows.append(row)
        return rows

    def from_jsonld(
        self, item: dict[str, Any], document: str, url: str, method: str | None = None,
    ) -> tuple[dict[str, Any], bool | None] | None:
        offer = jsonld.offer(item)
        price, currency = record_module.parse_price(offer.get("price"))
        if price is None:
            price, currency = record_module.parse_price(offer.get("lowPrice"))
        currency = domain.clean(offer.get("priceCurrency")) or currency or self.config.get("currency")
        categories = jsonld.breadcrumbs(document) or [domain.clean(item.get("category"))]
        categories = [value for value in categories if value]
        attributes = jsonld.specification_table(document)
        brand = jsonld.brand(item) or self.config.get("brand")
        images = jsonld.images(item, url)
        name = domain.clean(item.get("name")) or jsonld.meta(document, "og:title")
        description = domain.clean(item.get("description")) or jsonld.meta(document, "og:description")
        if not name:
            return None
        row = record_module.build(
            source=self.name,
            product_url=canonical(urljoin(url, domain.clean(item.get("url")) or url)),
            parent_url=canonical(url),
            name=name,
            brand=brand,
            manufacturer_sku=domain.manufacturer_code(brand, name, domain.clean(item.get("sku"))),
            supplier_reference=domain.clean(item.get("sku") or item.get("mpn")) or None,
            gtin=jsonld.gtin(item),
            description=description,
            category_path=categories or None,
            image_url=images[0] if images else jsonld.meta(document, "og:image"),
            all_image_urls=images or None,
            price=price,
            currency=currency,
            price_text=f"{offer.get('price')} {currency or ''}".strip() or None,
            vat=self.config.get("vat_status"),
            vat_rate=self.config.get("vat_rate"),
            availability=jsonld.availability(offer.get("availability")),
            technical_attributes=attributes or None,
            documents=domain.documents(jsonld.pdf_links(document, url), url) or None,
            extraction_method=method or self.method,
            source_detail_level="product_page",
            raw=item,
        )
        return row, self.category_allows(" ".join(categories), name)
