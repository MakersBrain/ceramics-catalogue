"""Explicit canary adapter from Wix neutral snapshots to legacy rows."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from mb_ceramics_catalogue.connectors import (
    CollectionRequest,
    RefreshMode,
    WixConnector,
    WixOptions,
)
from mb_ceramics_catalogue.datasets import ProjectionContext
from mb_ceramics_catalogue.datasets.ceramics import CeramicsCatalogueProjector
from mb_ceramics_catalogue.proxy import ProxyDenied

from .base import Blocked, Scraper


class _Transport:
    def __init__(self, owner: WixConnectorScraper) -> None:
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
                value = await self.owner.fetcher.render(url)
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
        except (httpx.HTTPError, Blocked, ProxyDenied, UnicodeError) as error:
            raise RuntimeError(type(error).__name__) from error


class WixConnectorScraper(Scraper):
    """Opt-in canary; production sources remain on the legacy ``wix`` key."""

    platform = "wix"
    method = "dom"

    def __init__(self, name: str, config: dict[str, Any], fetcher: Any) -> None:
        super().__init__(name, config, fetcher)
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True

    async def scrape(self, limit: int | None = None) -> Any:
        connector = WixConnector(
            _Transport(self),
            WixOptions(
                sitemaps=tuple(self.config.get("sitemaps") or ()),
                use_advertised_sitemaps=bool(
                    self.config.get("use_advertised_sitemaps", True)
                ),
                product_pattern=self.config.get("product_pattern"),
                page_limit=self.config.get("page_limit") or 500,
                sitemap_limit=self.config.get("sitemap_limit") or 100,
                brand=self.config.get("brand"),
                vat_status=self.config.get("vat_status"),
                render=self.config.get("render"),
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
        context = ProjectionContext(
            collection_id=f"legacy:{self.name}",
            source_id=self.name,
            dataset=projector.name,
            dataset_version=projector.version,
            projector_version=projector.projector_version,
            configuration={
                "scope": self.config.get("scope", 'materials'),
                "enrichments": self.config.get("enrichments") or [],
                "brand": self.config.get("brand"),
                "is_manufacturer": bool(self.config.get("is_manufacturer")),
                "extraction_method": self.method,
                "source_detail_level": "product_page",
                "apply_scope": False,
            },
        )

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
