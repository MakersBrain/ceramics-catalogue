"""Canary adapter from the neutral Shopify connector to legacy ScrapeResult."""

from __future__ import annotations

import asyncio
from typing import Any

from mb_ceramics_catalogue.connectors import CollectionRequest, RefreshMode
from mb_ceramics_catalogue.connectors.shopify import ShopifyConnector, ShopifyOptions
from mb_ceramics_catalogue.datasets import ProjectionContext
from mb_ceramics_catalogue.datasets.ceramics import CeramicsCatalogueProjector
from mb_ceramics_catalogue.pipeline.budget import RequestBudget, RequestCost
from mb_ceramics_catalogue.proxy import ProxyDenied

from .base import Blocked, Scraper


class _CountingFetcher:
    """Count successful legacy fetches while preserving its transport policy."""

    def __init__(self, fetcher: Any) -> None:
        self.fetcher = fetcher
        self.requests = 0
        self.failures: list[tuple[str, Exception]] = []

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
            self.failures.append((url, error))
            # The connector boundary is transport-neutral and treats runtime
            # fetch failures as typed incomplete-enumeration diagnostics.
            raise RuntimeError(str(error)) from error
        except Exception as error:
            self.failures.append((url, error))
            raise
        self.requests += 1
        return value

    async def text(self, url: str, *, headers: dict[str, str] | None = None) -> str:
        try:
            value = await self.fetcher.text(url, headers=headers)
        except (Blocked, ProxyDenied) as error:
            self.failures.append((url, error))
            raise RuntimeError(str(error)) from error
        except Exception as error:
            self.failures.append((url, error))
            raise
        self.requests += 1
        return value

    async def rotate_client(self) -> None:
        await self.fetcher.rotate_client()


class ShopifyConnectorScraper(Scraper):
    """Explicit opt-in path; the production ``shopify`` key remains legacy."""

    platform = "shopify"
    method = "api_json"

    def __init__(self, name: str, config: dict[str, Any], fetcher: Any) -> None:
        super().__init__(name, config, fetcher)
        self._cancel_requested = False

    def cancel(self) -> None:
        """Cooperatively stop before the next remote page."""
        self._cancel_requested = True

    async def scrape(self, limit: int | None = None) -> Any:
        counting = _CountingFetcher(self.fetcher)
        inventory_method = (
            "product_html"
            if self.config.get("inventory_product_html")
            else "product_json"
            if self.config.get("inventory_product_json")
            else "none"
        )
        options = ShopifyOptions(
            currency=self.config.get("currency"),
            vat_status=self.config.get("vat_status"),
            page_limit=self.config.get("page_limit") or 200,
            inventory_method=inventory_method,
            inventory_section_id=self.config.get("inventory_section_id"),
        )
        remaining = getattr(self.fetcher, "proxy_bytes_remaining", None)
        budget = (
            RequestBudget(RequestCost(http_requests=2**31 - 1, proxy_bytes=remaining))
            if inventory_method != "none" and isinstance(remaining, int) and remaining >= 0
            else None
        )
        connector = ShopifyConnector(counting, options, budget=budget)
        projector = CeramicsCatalogueProjector()
        request = CollectionRequest(
            source_id=self.name,
            base_url=self.base_url,
            refresh_mode=RefreshMode.FULL,
            requested_fields=projector.required_snapshot_fields,
            result_limit=limit,
            collections=tuple(self.config.get("collections") or ()),
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
                # Legacy Scraper.add owns category allowlists and exclusions.
                "apply_scope": False,
            },
        )

        priceless = 0
        try:
            async for page in connector.collect(request):
                self.result.discovered += page.discovered
                if not page.enumeration_intact:
                    self.result.truncated = True
                self._diagnostics(page.diagnostics)
                for snapshot in page.items:
                    category_match = self._category_match(snapshot)
                    for variant in snapshot.variants:
                        if not variant.offers:
                            priceless += 1
                    for typed in projector.project(snapshot, context):
                        self.add(typed.as_legacy_dict(), category_match)
        except asyncio.CancelledError:
            self.result.truncated = True
            raise
        finally:
            self.result.requests = counting.requests

        if self._cancel_requested:
            self.result.truncated = True
        if limit is not None and self.result.discovered >= limit:
            self.result.truncated = True
        for url, error in counting.failures:
            if url.endswith("/meta.json"):
                self.note(f"shop currency unavailable from meta.json ({error})")
        if priceless:
            self.note(
                f"{priceless} variants dropped without a price: "
                "the shop's currency could not be read from meta.json"
            )
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
        extensions = snapshot.platform_extensions
        tags = extensions.get("tags") or []
        tags = tags if isinstance(tags, list) else [tags]
        partition = snapshot.categories[0].name if len(snapshot.categories) > 1 else ""
        product_type = snapshot.categories[-1].name if snapshot.categories else ""
        return self.category_allows(
            product_type,
            " ".join(str(item) for item in tags),
            partition,
            extensions.get("handle") or "",
        )
