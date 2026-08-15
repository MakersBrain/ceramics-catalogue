"""Explicit canary adapters for bespoke neutral page connectors."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from mb_ceramics_catalogue.connectors import (
    AxnerConnector,
    AxnerOptions,
    CeramicoloursConnector,
    CeramicoloursOptions,
    CollectionRequest,
    KeramikKraftConnector,
    KeramikKraftOptions,
    RefreshMode,
)
from mb_ceramics_catalogue.datasets import ProjectionContext
from mb_ceramics_catalogue.datasets.ceramics import CeramicsCatalogueProjector
from mb_ceramics_catalogue.proxy import ProxyDenied

from .base import Blocked, BrowserUnavailable, Scraper


class _Transport:
    def __init__(self, owner: _BespokeConnectorScraper) -> None:
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
        except BrowserUnavailable:
            raise
        except (httpx.HTTPError, Blocked, ProxyDenied, UnicodeError) as error:
            raise RuntimeError(type(error).__name__) from error

    async def evaluate(
        self, url: str, script: str, *, wait_for: str | None = None
    ) -> Any:
        try:
            value = await self.owner.fetcher.evaluate_in_browser(
                url, script, wait_ms=1500, wait_for=wait_for
            )
            self.owner.result.rendered_pages += 1
            return value
        except BrowserUnavailable as error:
            # Pack prices are optional enrichment: the connector can preserve
            # the page's static published price when no browser worker exists.
            raise RuntimeError("BrowserUnavailable") from error
        except (Blocked, ProxyDenied, httpx.HTTPError) as error:
            raise RuntimeError(type(error).__name__) from error


class _BespokeConnectorScraper(Scraper):
    connector: Any
    method = "html"

    def __init__(self, name: str, config: dict[str, Any], fetcher: Any) -> None:
        super().__init__(name, config, fetcher)
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True

    def build_connector(self, transport: _Transport) -> Any:
        raise NotImplementedError

    async def scrape(self, limit: int | None = None) -> Any:
        projector = CeramicsCatalogueProjector()
        connector = self.build_connector(_Transport(self))
        request = CollectionRequest(
            source_id=self.name,
            base_url=self.base_url,
            refresh_mode=RefreshMode.FULL,
            requested_fields=projector.required_snapshot_fields,
            result_limit=limit,
            cancellation_check=lambda: self._cancel_requested,
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
                    parser = snapshot.platform_extensions.get("page_parser")
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
                            "extraction_method": (
                                parser if isinstance(parser, str) else self.method
                            ),
                            "source_detail_level": self.source_detail_level,
                            "apply_scope": False,
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

    @property
    def source_detail_level(self) -> str:
        return "product_page"


class AxnerConnectorScraper(_BespokeConnectorScraper):
    platform = "custom"
    method = "html"

    def build_connector(self, transport: _Transport) -> AxnerConnector:
        return AxnerConnector(
            transport,
            AxnerOptions(
                category_url=self.config.get("category_url"),
                category_page_limit=self.config.get("category_page_limit") or 400,
                page_limit=self.config.get("page_limit") or 500,
                brand=self.config.get("brand"),
                currency=self.config.get("currency") or "USD",
                vat_status=self.config.get("vat_status"),
                vat_rate=self.config.get("vat_rate"),
                render=self.config.get("render"),
            ),
        )


class CeramicoloursConnectorScraper(_BespokeConnectorScraper):
    platform = "custom"
    method = "dom"

    def build_connector(self, transport: _Transport) -> CeramicoloursConnector:
        return CeramicoloursConnector(
            transport,
            CeramicoloursOptions(
                category_ids=tuple(str(value) for value in self.config.get("category_ids") or ()),
                category_page_limit=self.config.get("category_page_limit") or 25,
                page_limit=self.config.get("page_limit") or 500,
                brand=self.config.get("brand"),
                vat_status=self.config.get("vat_status") or "inclusive",
                render=self.config.get("render"),
            ),
        )


class KeramikKraftConnectorScraper(_BespokeConnectorScraper):
    platform = "custom"
    method = "dom"

    @property
    def source_detail_level(self) -> str:
        return "listing"

    def build_connector(self, transport: _Transport) -> KeramikKraftConnector:
        return KeramikKraftConnector(
            transport,
            KeramikKraftOptions(
                category_paths=tuple(self.config.get("category_paths") or ()),
                category_page_limit=self.config.get("category_page_limit") or 150,
                page_limit=self.config.get("page_limit") or 500,
                brand=self.config.get("brand"),
                vat_rate=self.config.get("vat_rate"),
                render=self.config.get("render"),
            ),
        )
