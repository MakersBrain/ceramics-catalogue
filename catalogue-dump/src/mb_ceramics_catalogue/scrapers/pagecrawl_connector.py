"""Explicit canary adapter from PageCommerce neutral snapshots to legacy rows."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from mb_ceramics_catalogue.connectors import (
    CollectionRequest,
    PageCommerceConnector,
    PageCrawlOptions,
    RefreshMode,
)
from mb_ceramics_catalogue.datasets import ProjectionContext
from mb_ceramics_catalogue.datasets.ceramics import CeramicsCatalogueProjector
from mb_ceramics_catalogue.proxy import ProxyDenied

from .base import Blocked, BrowserUnavailable, Scraper


class _Transport:
    def __init__(self, owner: PageCrawlConnectorScraper) -> None:
        self.owner = owner

    async def advertised_sitemaps(self, base_url: str) -> tuple[str, ...]:
        try:
            _, sitemaps = await self.owner.fetcher.robots(base_url)
        except (httpx.HTTPError, Blocked, ProxyDenied) as error:
            raise RuntimeError(type(error).__name__) from error
        return tuple(sitemaps)

    async def document(
        self,
        url: str,
        *,
        rendered: bool = False,
        accept: str | None = None,
    ) -> str:
        try:
            if rendered:
                value = await self.owner.fetcher.render(
                    url, wait_for=self.owner.config.get("render_wait_for")
                )
                self.owner.result.rendered_pages += 1
                return value
            if accept is not None:
                response = await self.owner.fetcher.response(url, accept=accept)
                value = self.owner._sitemap_text(response)
            else:
                value = await self.owner.fetcher.text(
                    url,
                    browser_user_agent=True,
                    allow_proxy_fallback=(
                        self.owner.config.get("render") is False
                        or self.owner.fetcher.browser_policy == "never"
                    ),
                )
            self.owner.result.requests += 1
            return value
        except BrowserUnavailable:
            raise
        except (httpx.HTTPError, Blocked, ProxyDenied, UnicodeError) as error:
            raise RuntimeError(type(error).__name__) from error


class PageCrawlConnectorScraper(Scraper):
    """Opt-in canary; existing sources remain on the legacy ``pagecrawl`` key."""

    platform = "pagecrawl"
    method = "jsonld"

    def __init__(self, name: str, config: dict[str, Any], fetcher: Any) -> None:
        super().__init__(name, config, fetcher)
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True

    async def scrape(self, limit: int | None = None) -> Any:
        connector = PageCommerceConnector(
            _Transport(self),
            PageCrawlOptions(
                sitemaps=tuple(self.config.get("sitemaps") or ()),
                use_advertised_sitemaps=bool(
                    self.config.get("use_advertised_sitemaps", True)
                ),
                category_urls=tuple(self.config.get("category_urls") or ()),
                product_pattern=self.config.get("product_pattern"),
                pagination_patterns=tuple(self.config.get("pagination_patterns") or ()),
                card_links_only=bool(self.config.get("card_links_only")),
                page_limit=self.config.get("page_limit") or 500,
                sitemap_limit=self.config.get("sitemap_limit") or 100,
                category_page_limit=self.config.get("category_page_limit") or 120,
                render=self.config.get("render"),
                brand=self.config.get("brand"),
                currency=self.config.get("currency"),
                vat_status=self.config.get("vat_status"),
                vat_rate=self.config.get("vat_rate"),
                stock_from_quantity_maximum=bool(
                    self.config.get("stock_from_quantity_maximum")
                ),
            ),
        )
        projector = CeramicsCatalogueProjector()
        request = CollectionRequest(
            source_id=self.name,
            base_url=self.base_url,
            refresh_mode=RefreshMode.FULL,
            requested_fields=projector.required_snapshot_fields,
            result_limit=limit,
            cancellation_check=lambda: self._cancel_requested,
        )
        projection_configuration = {
            "scope": self.config.get("scope", 'materials'),
            "enrichments": self.config.get("enrichments") or [],
            "brand": self.config.get("brand"),
            "is_manufacturer": bool(self.config.get("is_manufacturer")),
            "source_detail_level": "product_page",
            "apply_scope": False,
        }
        saw_terminal = False
        try:
            async for page in connector.collect(request):
                self.result.discovered += page.discovered
                saw_terminal = page.terminal
                if not page.enumeration_intact:
                    self.result.truncated = True
                for diagnostic in page.diagnostics:
                    if diagnostic.severity == "error":
                        self.result.errors.append(
                            {
                                "url": diagnostic.url or self.base_url,
                                "error": diagnostic.message,
                            }
                        )
                    else:
                        self.note(diagnostic.message)
                for snapshot in page.items:
                    parser = snapshot.platform_extensions.get("page_parser")
                    context = ProjectionContext(
                        collection_id=f"legacy:{self.name}",
                        source_id=self.name,
                        dataset=projector.name,
                        dataset_version=projector.version,
                        projector_version=projector.projector_version,
                        configuration={
                            **projection_configuration,
                            "extraction_method": parser
                            if isinstance(parser, str)
                            else self.method,
                        },
                    )
                    category_match = self.category_allows(
                        " ".join(category.name for category in snapshot.categories),
                        snapshot.title,
                    )
                    for typed in projector.project(snapshot, context):
                        self.add(typed.as_legacy_dict(), category_match)
        except asyncio.CancelledError:
            self.result.truncated = True
            raise
        if self._cancel_requested and not saw_terminal:
            self.result.truncated = True
        if limit is not None and self.result.discovered >= limit:
            self.result.truncated = True
        return self.result
