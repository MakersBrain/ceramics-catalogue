"""Art4Fun, on the Swedish Starweb platform.

Starweb renders Product JSON-LD plus a variant table, and states the VAT basis
in the body class (`incl-vat` / `excl-vat`), so the tax context is observed
rather than assumed.
"""

from __future__ import annotations

import re
from typing import Any

from . import domain
from .pagecrawl import PageScraper


class StarwebScraper(PageScraper):
    platform = "starweb"
    method = "dom"

    def is_pagination(self, url: str) -> bool:
        return bool(re.search(r"[?&]page=\d+", url)) or super().is_pagination(url)

    def parse(self, document: str, url: str) -> list[tuple[dict[str, Any], bool | None]]:
        rows = super().parse(document, url)
        observed_vat = self.vat_from_markup(document)
        variants = self.variants(document)
        for row, _ in rows:
            if observed_vat:
                row["vat_status"] = observed_vat
                row["vat_basis"] = "page_markup"
            if variants:
                row["technical_attributes"] = (row.get("technical_attributes") or {}) | variants
        return rows

    @staticmethod
    def vat_from_markup(document: str) -> str | None:
        """Starweb states the displayed tax basis as a class on the page root."""
        if re.search(r'class=["\'][^"\']*\bincl-vat\b', document, re.I):
            return "inclusive"
        if re.search(r'class=["\'][^"\']*\bexcl-vat\b', document, re.I):
            return "exclusive"
        return None

    @staticmethod
    def variants(document: str) -> dict[str, str]:
        found: dict[str, str] = {}
        for match in re.finditer(
            r'<(?:label|span)[^>]*class=["\'][^"\']*(?:variant|attribute)-name[^"\']*["\'][^>]*>(.*?)</(?:label|span)>\s*'
            r'<(?:span|div)[^>]*class=["\'][^"\']*(?:variant|attribute)-value[^"\']*["\'][^>]*>(.*?)</(?:span|div)>',
            document, re.I | re.S,
        ):
            name, value = domain.clean(match.group(1)).rstrip(":"), domain.clean(match.group(2))
            if name and value:
                found[name] = value
        return found
