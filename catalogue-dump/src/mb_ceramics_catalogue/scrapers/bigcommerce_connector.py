"""Canary adapter from BigCommerceConnector to the legacy scraper result."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from mb_ceramics_catalogue.connectors import CollectionRequest, RefreshMode
from mb_ceramics_catalogue.connectors.bigcommerce import BigCommerceConnector, BigCommerceOptions
from mb_ceramics_catalogue.datasets import ProjectionContext
from mb_ceramics_catalogue.datasets.ceramics import CeramicsCatalogueProjector
from mb_ceramics_catalogue.proxy import ProxyDenied

from .base import Blocked, Scraper


class _Transport:
    def __init__(self, owner: BigCommerceConnectorScraper) -> None:
        self.owner = owner

    async def document(self, url: str, *, rendered: bool = False) -> str:
        try:
            if rendered:
                value = await self.owner.fetcher.render(url, wait_ms=2500)
                self.owner.result.rendered_pages += 1
                return value
            value = await self.owner.fetcher.text(url, browser_user_agent=True)
            self.owner.result.requests += 1
            return value
        except (Blocked, ProxyDenied) as error:
            raise RuntimeError(type(error).__name__) from error

    async def request_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        body: dict[str, Any],
        browser_context_url: str | None = None,
    ) -> Any:
        try:
            if browser_context_url is not None:
                payload = await self.owner.fetcher.request_json_in_browser(
                    browser_context_url,
                    url,
                    headers=headers,
                    body=body,
                )
            else:
                response = await self.owner.fetcher.response(
                    url,
                    method="POST",
                    json_body=body,
                    headers=headers,
                    browser_user_agent=True,
                )
                payload = response.json()
            self.owner.result.requests += 1
            return payload
        except (Blocked, ProxyDenied) as error:
            raise RuntimeError(type(error).__name__) from error


class BigCommerceConnectorScraper(Scraper):
    """Explicit opt-in; ``bigcommerce`` continues to select the legacy path."""

    platform = "bigcommerce"
    method = "graphql"

    def __init__(self, name: str, config: dict[str, Any], fetcher: Any) -> None:
        super().__init__(name, config, fetcher)
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True

    async def scrape(self, limit: int | None = None) -> Any:
        projector = CeramicsCatalogueProjector()
        connector = BigCommerceConnector(
            _Transport(self),
            BigCommerceOptions(
                token_page=self.config.get("category_url"),
                page_limit=self.config.get("page_limit") or 200,
                vat_status=self.config.get("vat_status"),
            ),
        )
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
                "source_detail_level": "api",
                "apply_scope": False,
            },
        )
        try:
            async for page in connector.collect(request):
                self.result.discovered += page.discovered
                if not page.enumeration_intact:
                    self.result.truncated = True
                for diagnostic in page.diagnostics:
                    self.result.errors.append(
                        {"url": diagnostic.url or self.base_url, "error": diagnostic.message}
                    )
                for snapshot in page.items:
                    category_match = self.category_allows(
                        " ".join(item.name for item in snapshot.categories), snapshot.title
                    )
                    for typed in projector.project(snapshot, context):
                        self.add(typed.as_legacy_dict(), category_match)
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
