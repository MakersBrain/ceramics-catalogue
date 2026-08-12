"""PrestaShop storefronts.

PrestaShop renders a JSON product object into the page for its own JavaScript
(`#product-details[data-product]`). That object carries the reference, stock,
features, every image and the attachment list, which is far more than the
JSON-LD block exposes, so it is preferred wherever the theme provides it.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

from . import domain, jsonld
from . import record as record_module
from .pagecrawl import PageScraper, canonical


def data_product(document: str) -> dict[str, Any] | None:
    """Decode the JSON product object PrestaShop embeds for its own scripts."""
    for pattern in (
        r'<[^>]+\bid=["\']product-details["\'][^>]+\bdata-product=["\']([^"\']+)["\']',
        r'<[^>]+\bdata-product=["\']([^"\']+)["\'][^>]+\bid=["\']product-details["\']',
    ):
        if match := re.search(pattern, document, re.I | re.S):
            try:
                value = json.loads(html.unescape(match.group(1)))
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(value, dict):
                return value
    return None


def variant_groups(document: str) -> dict[str, dict[str, Any]]:
    """The attribute options a product page offers, per attribute group.

    Returns `{group_id: {"selected": attribute_id, "options": [attribute_id]}}`.
    Radio themes and select themes both appear in the wild.
    """
    groups: dict[str, dict[str, Any]] = {}
    for match in re.finditer(r'<ul[^>]+id=["\']group_(\d+)["\'](.*?)</ul>', document, re.I | re.S):
        group_id, body = match.group(1), match.group(2)
        options, selected = [], None
        for tag in re.finditer(r"<input[^>]*\binput-radio\b[^>]*>", body, re.I):
            value = re.search(r'\bvalue=["\'](\d+)["\']', tag.group(0))
            if not value:
                continue
            options.append(value.group(1))
            if re.search(r"\bchecked\b", tag.group(0), re.I):
                selected = value.group(1)
        if options:
            groups[group_id] = {"selected": selected or options[0], "options": options}
    for match in re.finditer(
        r'<select[^>]+name=["\']group\[(\d+)\]["\'](.*?)</select>', document, re.I | re.S,
    ):
        group_id, body = match.group(1), match.group(2)
        options, selected = [], None
        for tag in re.finditer(r"<option[^>]*>", body, re.I):
            value = re.search(r'\bvalue=["\'](\d+)["\']', tag.group(0))
            if not value:
                continue
            options.append(value.group(1))
            if re.search(r"\bselected\b", tag.group(0), re.I):
                selected = value.group(1)
        if options:
            groups.setdefault(group_id, {"selected": selected or options[0], "options": options})
    return groups


class PrestaShopScraper(PageScraper):
    platform = "prestashop"
    method = "dom"

    def parse(self, document: str, url: str) -> list[tuple[dict[str, Any], bool | None]]:
        details = data_product(document)
        if details is None:
            return super().parse(document, url)
        row = self.from_details(details, document, url)
        return [row] if row else []

    async def parse_page(self, document: str, url: str) -> list[tuple[dict[str, Any], bool | None]]:
        """Read the page, then every other size the product is sold in.

        A PrestaShop product page renders one combination — usually the default,
        in-stock one — and the rest exist only behind the variant selector. Left
        alone that silently drops sizes, and it drops them selectively: an
        out-of-stock 3.8 L never appears while its in-stock 472 ml sibling does,
        which would make the dump read as if the large size were not sold.
        """
        rows = self.parse(document, url)
        if not rows or not self.config.get("variant_combinations", True):
            return rows
        details = data_product(document)
        if details is None:
            return rows
        for extra in await self.combinations(document, url, details):
            row = self.from_details(extra, document, url)
            if row:
                rows.append(row)
        return rows

    async def combinations(
        self, document: str, url: str, details: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Fetch the combinations the page did not render.

        PrestaShop's own selector calls `?group[N]=X&ajax=1&action=refresh`, and
        the JSON it answers with carries a complete product object for that
        combination — price, reference and stock included. One option is varied
        at a time against the rendered selection rather than walking the whole
        cartesian product, which keeps the request count linear in the number of
        options; every source here sells by a single "Size" group.
        """
        groups = variant_groups(document)
        if not groups or all(len(group["options"]) < 2 for group in groups.values()):
            return []

        # Ask the canonical URL the page reports, not the one that was fetched:
        # the sitemap still lists an older category path, and the redirect to
        # the current one drops the query string, so a combination request built
        # on the fetched URL comes back as an ordinary page and is discarded.
        base = domain.clean(details.get("link")) or url

        wanted = []
        for group_id, group in groups.items():
            for option in group["options"]:
                if option == group["selected"]:
                    continue
                query = {f"group[{other}]": data["selected"] for other, data in groups.items()}
                query[f"group[{group_id}]"] = option
                query |= {"ajax": "1", "action": "refresh", "quantity_wanted": "1"}
                separator = "&" if urlparse(base).query else "?"
                wanted.append(f"{base}{separator}{urlencode(query)}")

        # The sizes of one product are independent requests, so they go out
        # together and the host limiter decides the actual pace. Fetching them
        # one after another would make a four-size product four times slower
        # than a single-size one for no reason.
        bodies = await asyncio.gather(*(self.load(target) for target in wanted))

        seen = {str(details.get("id_product_attribute") or "")}
        found = []
        for body in bodies:
            if not body:
                continue
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                continue
            extra = data_product(payload.get("product_details") or "")
            if extra is None:
                continue
            identifier = str(extra.get("id_product_attribute") or "")
            if identifier in seen:
                continue
            seen.add(identifier)
            # The fragment carries no canonical link of its own.
            extra.setdefault("link", domain.clean(payload.get("product_url")) or url)
            found.append(extra)
        return found

    def features(self, details: dict[str, Any]) -> dict[str, str]:
        return {
            domain.clean(feature.get("name")): domain.clean(feature.get("value"))
            for feature in details.get("features") or []
            if isinstance(feature, dict) and feature.get("name") and feature.get("value")
        }

    def attachments(self, details: dict[str, Any], document: str, url: str) -> list[dict[str, Any]]:
        links = {
            match.group(2): (urljoin(url, html.unescape(match.group(1))), domain.clean(match.group(3)))
            for match in re.finditer(
                r'<a[^>]+href=["\']([^"\']*id_attachment=(\d+)[^"\']*)["\'][^>]*>(.*?)</a>',
                document, re.I | re.S,
            )
        }
        pairs = []
        for attachment in details.get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            identifier = str(attachment.get("id_attachment") or "")
            label = domain.clean(attachment.get("file_name") or attachment.get("name"))
            href, anchor = links.get(identifier, ("", ""))
            if href:
                pairs.append((href, label or anchor))
        return domain.documents(pairs or jsonld.pdf_links(document, url), url)

    def images(self, details: dict[str, Any]) -> list[str]:
        found = []
        for image in details.get("images") or []:
            if not isinstance(image, dict):
                continue
            candidate = domain.clean((image.get("large") or {}).get("url") or image.get("url"))
            if candidate:
                found.append(candidate)
        return list(dict.fromkeys(found))

    def from_details(
        self, details: dict[str, Any], document: str, url: str,
    ) -> tuple[dict[str, Any], bool | None] | None:
        name = domain.clean(details.get("name"))
        if not name:
            return None
        price, currency = record_module.parse_price(details.get("price_amount") or details.get("price"))
        if price is None:
            offer = jsonld.offer(next(iter(jsonld.products(document)), {}))
            price, currency = record_module.parse_price(offer.get("price"))
            currency = domain.clean(offer.get("priceCurrency")) or currency
        if price is None:
            return None
        features = self.features(details)
        categories = jsonld.breadcrumbs(document) or [domain.clean(details.get("category_name"))]
        categories = [value for value in categories if value]
        brand = domain.clean(details.get("manufacturer_name")) or features.get("Brand") or self.config.get("brand")
        images = self.images(details)
        description = domain.clean(details.get("description")) or domain.clean(details.get("description_short"))
        combination = self.combination(details)
        row = record_module.build(
            source=self.name,
            product_url=canonical(domain.clean(details.get("link")) or url),
            parent_url=canonical(url),
            variant_id=str(details.get("id_product_attribute") or "") or None,
            variant_title=combination or None,
            name=name,
            brand=brand,
            manufacturer_sku=domain.manufacturer_code(brand, name, self.reference(details)),
            supplier_reference=self.reference(details),
            gtin=domain.clean(details.get("ean13") or details.get("upc")) or None,
            description=description,
            category_path=categories or None,
            image_url=images[0] if images else None,
            all_image_urls=images or None,
            price=price,
            currency=currency or self.config.get("currency", "EUR"),
            price_text=domain.clean(details.get("price")) or None,
            list_price=self._regular_price(details, price),
            vat=self.config.get("vat_status"),
            vat_rate=self.config.get("vat_rate"),
            availability=(
                "https://schema.org/InStock" if (details.get("quantity") or 0) > 0
                else "https://schema.org/OutOfStock"
            ),
            stock_quantity=details.get("quantity") if isinstance(details.get("quantity"), int) else None,
            technical_attributes=(features | jsonld.specification_table(document)) or None,
            documents=self.attachments(details, document, url) or None,
            extraction_method=self.method,
            source_detail_level="product_page",
            source_updated_at=domain.clean(details.get("date_upd")) or None,
            raw=details,
        )
        return row, self.category_allows(" ".join(categories), name)

    @staticmethod
    def reference(details: dict[str, Any]) -> str | None:
        """The reference of the size this row describes.

        PrestaShop keeps one reference on the product and another on each
        combination. The product-level one is the same for every size, so
        reading it makes a 472 ml jar and a 3.8 L pot look like one article.
        """
        for entry in (details.get("attributes") or {}).values():
            if isinstance(entry, dict) and (value := domain.clean(entry.get("reference"))):
                return value
        return domain.clean(details.get("reference")) or None

    @staticmethod
    def combination(details: dict[str, Any]) -> str:
        """The selected combination, keyed by attribute-group id.

        PrestaShop 1.7 publishes the group label as `group` and the chosen value
        as `name` (`{"group": "Size", "name": "472 ml"}`); older themes publish
        `{"name": "Size", "value": "472 ml"}`. Reading only the second shape
        loses the package size on every shop that uses the first, which is where
        1240-design keeps it: the size is in the combination and nowhere else,
        so a missed label costs the unit price too.
        """
        parts = []
        for entry in (details.get("attributes") or {}).values():
            if not isinstance(entry, dict):
                continue
            if entry.get("group"):
                label, value = entry.get("group"), entry.get("name")
            else:
                label, value = entry.get("name"), entry.get("value")
            label, value = domain.clean(label), domain.clean(value)
            if value:
                parts.append(f"{label}: {value}" if label else value)
        return ", ".join(parts)

    @staticmethod
    def _regular_price(details: dict[str, Any], price: float | None) -> float | None:
        regular, _ = record_module.parse_price(details.get("regular_price_amount") or details.get("regular_price"))
        return regular if regular and regular != price else None


class Sio2Scraper(PrestaShopScraper):
    """SIO-2, narrowed to its own clay bodies and materials plus every stocked glaze.

    Tools, equipment, bisque, books, acrylics, cold glazes and lustres are out of
    scope. For materials SIO-2 manufactures, only SIO-2-branded items are kept;
    in the glaze and underglaze categories every brand is kept.
    """

    OWN_MATERIAL_CATEGORIES = frozenset({
        "low-fire-ceramic-clays", "high-fire-ceramic-clays", "specialty-ceramic-clays",
        "porcelain", "3d-printing-ceramic-clays", "modelling-clay", "liquid-clay",
        "other-materials", "raw-materials-and-oxides", "auxiliary-products",
    })
    GLAZE_CATEGORIES = frozenset({
        "prepared-glazes", "powdered-glazes", "prepared-underglazes", "powdered-underglazes",
    })

    @classmethod
    def _category_of(cls, url: str) -> str:
        parts = urlparse(url).path.strip("/").split("/")
        return parts[-2] if len(parts) >= 2 else ""

    @classmethod
    def _family(cls, category: str) -> str:
        if "underglazes" in category:
            return "underglaze"
        if category in cls.GLAZE_CATEGORIES:
            return "glaze"
        if category in {"other-materials", "raw-materials-and-oxides", "auxiliary-products"}:
            return "material"
        return "clay"

    def parse(self, document: str, url: str) -> list[tuple[dict[str, Any], bool | None]]:
        category = self._category_of(url)
        if category not in self.OWN_MATERIAL_CATEGORIES | self.GLAZE_CATEGORIES:
            return []
        rows = super().parse(document, url)
        kept = []
        for row, _ in rows:
            brand = domain.fold(row.get("brand")).replace("®", "")
            if category in self.OWN_MATERIAL_CATEGORIES and brand != "sio-2":
                continue
            row["family"] = row.get("family") or self._family(category)
            row["material_kind"] = category
            # An allowlisted SIO-2 category is authoritative for scope.
            kept.append((row, True))
        return kept
