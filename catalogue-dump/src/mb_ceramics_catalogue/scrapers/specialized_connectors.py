"""Legacy-result canaries for specialized neutral page connectors."""

from __future__ import annotations

import asyncio
from typing import Any

from mb_ceramics_catalogue.config.sources import SourceConfig
from mb_ceramics_catalogue.connectors import CollectionRequest, RefreshMode
from mb_ceramics_catalogue.datasets import CeramicsCatalogueProjector, ProjectionContext
from mb_ceramics_catalogue.ops.connector_adapters import runtime_plan

from .base import Scraper


class SpecializedConnectorScraper(Scraper):
    legacy_key: str
    method = "dom"

    def __init__(self, name: str, config: dict[str, Any], fetcher: Any) -> None:
        super().__init__(name, config, fetcher)
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True

    async def scrape(self, limit: int | None = None) -> Any:
        source = SourceConfig.model_validate({**self.config, "scraper": self.legacy_key})
        plan = runtime_plan(source)
        connector = plan.build(self.fetcher, None)
        projector = CeramicsCatalogueProjector()
        request = CollectionRequest(
            source_id=self.name, base_url=self.base_url, refresh_mode=RefreshMode.FULL,
            requested_fields=projector.required_snapshot_fields, result_limit=limit,
            categories=plan.categories, collections=plan.collections,
            cancellation_check=lambda: self._cancel_requested,
        )
        context = ProjectionContext(
            collection_id=f"legacy:{self.name}", source_id=self.name,
            dataset=projector.name, dataset_version=projector.version,
            projector_version=projector.projector_version,
            configuration={
                "scope": self.config.get("scope", 'materials'),
                "enrichments": self.config.get("enrichments") or [],
                "brand": self.config.get("brand"),
                "is_manufacturer": bool(self.config.get("is_manufacturer")),
                "extraction_method": plan.extraction_method,
                "source_detail_level": plan.source_detail_level,
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
                        self.result.errors.append({
                            "url": diagnostic.url or self.base_url,
                            "error": diagnostic.message,
                        })
                    else:
                        self.note(diagnostic.message)
                for snapshot in page.items:
                    category_match = self.category_allows(
                        " ".join(category.name for category in snapshot.categories), snapshot.title
                    )
                    for record in projector.project(snapshot, context):
                        self.add(record.as_legacy_dict(), category_match)
        except asyncio.CancelledError:
            self.result.truncated = True
            raise
        if self._cancel_requested:
            self.result.truncated = True
        return self.result


class ShopwareConnectorScraper(SpecializedConnectorScraper):
    legacy_key = platform = "shopware"
    method = "dom"


class SumUpConnectorScraper(SpecializedConnectorScraper):
    legacy_key = platform = "sumup"
    method = "dom"


class StarwebConnectorScraper(SpecializedConnectorScraper):
    legacy_key = platform = "starweb"
    method = "dom"


class NitroSellConnectorScraper(SpecializedConnectorScraper):
    legacy_key = platform = "nitrosell"
    method = "opengraph"
