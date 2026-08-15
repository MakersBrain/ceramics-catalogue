"""Explicit canary adapter for the neutral PrestaShop connector."""

from __future__ import annotations

import asyncio
import gzip
from typing import Any

import httpx

from mb_ceramics_catalogue.connectors import CollectionRequest, RefreshMode
from mb_ceramics_catalogue.connectors.prestashop import PrestaShopConnector, PrestaShopOptions
from mb_ceramics_catalogue.datasets import ProjectionContext
from mb_ceramics_catalogue.datasets.ceramics import CeramicsCatalogueProjector
from mb_ceramics_catalogue.datasets.ceramics.policies import Sio2ProjectionPolicy
from mb_ceramics_catalogue.proxy import ProxyDenied

from .base import Blocked, BrowserUnavailable, Scraper


class _Transport:
    def __init__(self, owner: PrestaShopConnectorScraper) -> None:
        self.owner = owner

    async def allowed(self, url: str) -> bool:
        return await self.owner.fetcher.may_fetch(
            url,
            self.owner.ignore_robots,
            self.owner.obey_robots,
        )

    async def advertised_sitemaps(self, base_url: str) -> tuple[str, ...]:
        _, sitemaps = await self.owner.fetcher.robots(base_url)
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
                body = response.content
                if url.lower().endswith(".gz") and body.startswith(b"\x1f\x8b"):
                    body = gzip.decompress(body)
                value = body.decode(response.encoding or "utf-8", errors="replace")
            else:
                value = await self.owner.fetcher.text(url, browser_user_agent=True)
            self.owner.result.requests += 1
            return value
        except BrowserUnavailable:
            raise
        except (Blocked, ProxyDenied) as error:
            raise RuntimeError(type(error).__name__) from error


class PrestaShopConnectorScraper(Scraper):
    """Opt-in canary; ``prestashop`` continues to use the legacy scraper."""

    platform = "prestashop"
    method = "dom"

    def __init__(self, name: str, config: dict[str, Any], fetcher: Any) -> None:
        super().__init__(name, config, fetcher)
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True

    async def scrape(self, limit: int | None = None) -> Any:
        projector = CeramicsCatalogueProjector()
        connector = PrestaShopConnector(
            _Transport(self),
            PrestaShopOptions(
                sitemaps=tuple(self.config.get("sitemaps") or ()),
                category_urls=tuple(self.config.get("category_urls") or ()),
                use_advertised_sitemaps=self.config.get("use_advertised_sitemaps", True),
                product_pattern=self.config.get("product_pattern"),
                card_links_only=bool(self.config.get("card_links_only")),
                pagination_patterns=tuple(self.config.get("pagination_patterns") or ()),
                render=self.config.get("render"),
                variant_combinations=self.config.get("variant_combinations", True),
                currency=self.config.get("currency") or "EUR",
                brand=self.config.get("brand"),
                vat_status=self.config.get("vat_status"),
                vat_rate=self.config.get("vat_rate"),
                page_limit=self.config.get("page_limit") or 500,
                category_page_limit=self.config.get("category_page_limit") or 120,
                sitemap_page_limit=self.config.get("sitemap_page_limit") or 500,
                combination_limit=self.config.get("combination_limit") or 30,
            ),
        )
        request = CollectionRequest(
            source_id=self.name,
            base_url=self.base_url,
            refresh_mode=RefreshMode.FULL,
            requested_fields=projector.required_snapshot_fields,
            result_limit=limit,
            request_budget=self.config.get("request_budget"),
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
        try:
            async for page in connector.collect(request):
                self.result.discovered += page.discovered
                if not page.enumeration_intact:
                    self.result.truncated = True
                for diagnostic in page.diagnostics:
                    if diagnostic.severity == "error":
                        self.result.errors.append(
                            {"url": diagnostic.url or self.base_url, "error": diagnostic.message}
                        )
                    else:
                        self.note(diagnostic.message)
                for snapshot in page.items:
                    category_match = self.category_allows(
                        " ".join(category.name for category in snapshot.categories),
                        snapshot.title,
                    )
                    for record in projector.project(snapshot, context):
                        self._add_projected(record.as_legacy_dict(), snapshot, category_match)
        except asyncio.CancelledError:
            self.result.truncated = True
            raise
        except httpx.HTTPError as error:
            self.enumeration_failed(self.base_url, error)
        if self._cancel_requested:
            self.result.truncated = True
        if limit is not None and self.result.discovered >= limit:
            self.result.truncated = True
        return self.result

    def _add_projected(
        self, row: dict[str, Any], snapshot: Any, category_match: bool | None
    ) -> None:
        self.add(row, category_match)


class Sio2ConnectorScraper(PrestaShopConnectorScraper):
    """SIO-2 canary using the neutral parser plus its typed projection policy."""

    def __init__(self, name: str, config: dict[str, Any], fetcher: Any) -> None:
        super().__init__(name, config, fetcher)
        self.policy = Sio2ProjectionPolicy()

    def _add_projected(
        self, row: dict[str, Any], snapshot: Any, category_match: bool | None
    ) -> None:
        if projected := self.policy.apply(row, snapshot.canonical_url):
            self.add(projected, True)
