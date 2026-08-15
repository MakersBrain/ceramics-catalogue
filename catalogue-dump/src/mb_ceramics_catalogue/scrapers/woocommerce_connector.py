"""Explicit canary adapter from WooCommerce neutral snapshots to legacy rows."""

from __future__ import annotations

import asyncio
from typing import Any

from mb_ceramics_catalogue.connectors import (
    CollectionRequest,
    RefreshMode,
    WooCommerceConnector,
    WooCommerceOptions,
)
from mb_ceramics_catalogue.datasets import (
    CeramicsCatalogueProjector,
    CeramicsIdentityProjector,
    ProjectionContext,
)
from mb_ceramics_catalogue.proxy import ProxyDenied

from .base import Blocked, Scraper


class _CountingFetcher:
    def __init__(self, fetcher: Any) -> None:
        self.fetcher = fetcher
        self.requests = 0

    async def json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        try:
            value = await self.fetcher.json(url, params=params, headers=headers)
        except (Blocked, ProxyDenied) as error:
            raise RuntimeError(str(error)) from error
        self.requests += 1
        return value


class WooCommerceConnectorScraper(Scraper):
    """Opt-in canary; production sources remain on ``woocommerce``."""

    platform = "woocommerce"
    method = "api_json"

    def __init__(self, name: str, config: dict[str, Any], fetcher: Any) -> None:
        super().__init__(name, config, fetcher)
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True

    async def scrape(self, limit: int | None = None) -> Any:
        counting = _CountingFetcher(self.fetcher)
        options = WooCommerceOptions(
            store_categories=tuple(self.config.get("store_categories") or ()),
            identity_only=bool(self.config.get("identity_only")),
            brand=self.config.get("brand"),
            vat_status=self.config.get("vat_status"),
            vat_rate=self.config.get("vat_rate"),
            stock_from_add_to_cart_maximum=bool(
                self.config.get("stock_from_add_to_cart_maximum")
            ),
            page_limit=self.config.get("page_limit") or 100,
            variation_page_limit=self.config.get("variation_page_limit") or 200,
            category_page_limit=self.config.get("category_page_limit") or 20,
        )
        connector = WooCommerceConnector(counting, options)
        projector = (
            CeramicsIdentityProjector()
            if options.identity_only
            else CeramicsCatalogueProjector()
        )
        request = CollectionRequest(
            source_id=self.name,
            base_url=self.base_url,
            refresh_mode=RefreshMode.FULL,
            requested_fields=projector.required_snapshot_fields,
            result_limit=limit,
            categories=options.store_categories,
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
                "source_detail_level": "api",
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
                self._diagnostics(page.diagnostics)
                for snapshot in page.items:
                    category_match = self._category_match(snapshot)
                    for typed in projector.project(snapshot, context):
                        self.add(typed.as_legacy_dict(), category_match)
        except asyncio.CancelledError:
            self.result.truncated = True
            raise
        finally:
            self.result.requests = counting.requests

        if self._cancel_requested and not saw_terminal:
            self.result.truncated = True
        if limit is not None and self.result.discovered >= limit:
            self.result.truncated = True
        return self.result

    def _diagnostics(self, diagnostics: tuple[Any, ...]) -> None:
        for diagnostic in diagnostics:
            if diagnostic.affects_completeness:
                self.result.truncated = True
            if diagnostic.severity == "error":
                self.result.errors.append(
                    {"url": diagnostic.url or self.base_url, "error": diagnostic.message}
                )
            else:
                self.note(diagnostic.message)

    def _category_match(self, snapshot: Any) -> bool | None:
        slugs = snapshot.platform_extensions.get("category_slugs") or []
        categories = [category.name for category in snapshot.categories]
        return self.category_allows(
            " ".join(str(value) for value in (*slugs, *categories)),
            snapshot.title,
        )
