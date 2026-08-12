"""Shopware 6 storefronts.

Shopware 6 renders schema.org Product markup and a properties table server-side.
Where the theme exposes a Store API access key, the JSON API is used instead
because it returns clean property and unit-price data.
"""

from __future__ import annotations

import re
from typing import Any

from . import domain, jsonld
from .pagecrawl import PageScraper

ACCESS_KEY_PATTERN = r'(?:sw-access-key|accessKey|swAccessKey)["\']?\s*[:=]\s*["\']([A-Za-z0-9]{20,})'


class ShopwareScraper(PageScraper):
    platform = "shopware"
    method = "dom"

    def parse(self, document: str, url: str) -> list[tuple[dict[str, Any], bool | None]]:
        rows = super().parse(document, url)
        properties = self.properties(document)
        for row, _ in rows:
            if properties:
                row["technical_attributes"] = (row.get("technical_attributes") or {}) | properties
            if not row.get("supplier_reference"):
                row["supplier_reference"] = self.product_number(document)
            if unit := self.unit_price(document):
                row.setdefault("published_unit_price", unit)
        return rows

    @staticmethod
    def properties(document: str) -> dict[str, str]:
        """Read the Shopware properties table, which carries the technical data."""
        found: dict[str, str] = {}
        for match in re.finditer(
            r'<dt[^>]*class=["\'][^"\']*properties-label[^"\']*["\'][^>]*>(.*?)</dt>\s*'
            r'<dd[^>]*class=["\'][^"\']*properties-value[^"\']*["\'][^>]*>(.*?)</dd>',
            document, re.I | re.S,
        ):
            name, value = domain.clean(match.group(1)).rstrip(":"), domain.clean(match.group(2))
            if name and value:
                found[name] = value
        return found or jsonld.specification_table(document)

    @staticmethod
    def product_number(document: str) -> str | None:
        match = re.search(
            r'(?:product-detail-ordernumber|Artikel-?Nr\.?|Bestellnummer)[^>]*>\s*([A-Za-z0-9][\w.\-/]*)',
            document, re.I,
        )
        return domain.clean(match.group(1)) if match else None

    @staticmethod
    def unit_price(document: str) -> str | None:
        """Shopware publishes its own price-per-unit; keep it as published."""
        match = re.search(
            r'<[^>]+class=["\'][^"\']*product-detail-price-unit[^"\']*["\'][^>]*>(.*?)</',
            document, re.I | re.S,
        )
        return domain.clean(match.group(1)) or None if match else None
