"""Wix Stores storefronts.

Wix publishes a store-products sitemap and renders the page server-side, but its
Product JSON-LD carries no offers. The prices, SKU and variants live in the
warmup data the page ships for its own scripts, keyed by product slug, so that
is what this scraper reads. JSON-LD is still used for the name and images.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import unquote, urlparse

from . import domain, jsonld
from . import record as record_module
from .pagecrawl import PageScraper, canonical


def balanced_object(document: str, start: int) -> dict[str, Any] | None:
    """Read one complete JSON object beginning at `start`, respecting strings."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(document)):
        character = document[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(document[start:index + 1])
                except json.JSONDecodeError:
                    return None
    return None


class WixScraper(PageScraper):
    platform = "wix"
    method = "dom"

    async def discover_from_sitemaps(self) -> list[str]:
        urls = await super().discover_from_sitemaps()
        if urls:
            return urls
        # Wix publishes this sitemap even when robots.txt does not advertise it.
        found = await self.sitemap_urls([f"{self.origin()}/store-products-sitemap.xml"])
        return [canonical(url) for url in found if self.is_product_url(url)]

    def warmup_product(self, document: str, url: str) -> dict[str, Any] | None:
        """Find the product object Wix ships for the slug in this URL."""
        slug = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
        marker = f'"{slug}":{{"product":{{'
        index = document.find(marker)
        if index < 0:
            # Some themes key the entry differently; fall back to the first one.
            match = re.search(r'"product":\{"id":"[0-9a-f-]{36}"', document)
            if not match:
                return None
            return balanced_object(document, match.start() + len('"product":'))
        return balanced_object(document, index + len(f'"{slug}":{{"product":'))

    def parse(self, document: str, url: str) -> list[tuple[dict[str, Any], bool | None]]:
        product = self.warmup_product(document, url)
        if product is None:
            return super().parse(document, url)

        item = next(iter(jsonld.products(document)), {})
        name = domain.clean(product.get("name")) or domain.clean(item.get("name")) or jsonld.meta(document, "og:title")
        if not name:
            return []
        description = domain.clean(product.get("description"))
        images = jsonld.images(item, url) or self._images(product)
        currency = self._currency(document)
        brand = domain.clean(product.get("brand")) or self.config.get("brand")
        documents = domain.documents(jsonld.pdf_links(document, url), url)

        common = dict(
            source=self.name,
            product_url=canonical(url),
            parent_url=canonical(url),
            product_name=name,
            brand=brand,
            description=description or None,
            category_path=jsonld.breadcrumbs(document) or None,
            image_url=images[0] if images else None,
            all_image_urls=images or None,
            currency=currency,
            vat=self.config.get("vat_status"),
            documents=documents or None,
            extraction_method=self.method,
            source_detail_level="product_page",
        )

        rows = []
        for variant in self._variants(product):
            price = variant.get("price")
            if not isinstance(price, (int, float)):
                continue
            title = variant.get("title")
            rows.append((
                record_module.build(
                    **common,
                    variant_id=str(variant.get("id") or "") or None,
                    variant_title=title,
                    name=f"{name} {title}".strip() if title else name,
                    manufacturer_sku=domain.manufacturer_code(brand, name, variant.get("sku") or ""),
                    supplier_reference=domain.clean(variant.get("sku") or product.get("sku")) or None,
                    price=float(price),
                    price_text=domain.clean(variant.get("formattedPrice")) or None,
                    list_price=self._compare_price(variant),
                    availability=(
                        "https://schema.org/InStock" if variant.get("in_stock", True)
                        else "https://schema.org/OutOfStock"
                    ),
                    stock_quantity=variant.get("stock_quantity"),
                    technical_attributes=variant.get("options") or None,
                    raw={"product": {k: v for k, v in product.items() if k != "productItems"}, "variant": variant},
                ),
                self.category_allows(" ".join(jsonld.breadcrumbs(document)), name),
            ))
        return rows

    def _variants(self, product: dict[str, Any]) -> list[dict[str, Any]]:
        """Expand Wix productItems, falling back to the product's own price."""
        items = [item for item in product.get("productItems") or [] if isinstance(item, dict)]
        variants = []
        for item in items:
            options = {
                domain.clean(key): domain.clean(value)
                for key, value in (item.get("optionsSelections") or {}).items()
            } if isinstance(item.get("optionsSelections"), dict) else {}
            title = ", ".join(value for value in options.values() if value)
            variants.append({
                "id": item.get("id"),
                "title": title or None,
                "sku": item.get("sku") or product.get("sku"),
                "price": item.get("price", product.get("price")),
                "comparePrice": item.get("comparePrice"),
                "formattedPrice": item.get("formattedPrice"),
                "in_stock": item.get("isInStock", item.get("inStock", True)),
                "stock_quantity": self._stock_quantity(item, product),
                "options": options or None,
            })
        # A single placeholder item carries no real variation; treat it as the product.
        if len(variants) <= 1:
            return [{
                "id": None,
                "title": None,
                "sku": product.get("sku"),
                "price": product.get("price"),
                "comparePrice": product.get("comparePrice"),
                "formattedPrice": product.get("formattedPrice"),
                "in_stock": product.get("isInStock", True),
                "stock_quantity": self._stock_quantity(product),
                "options": None,
            }]
        return variants

    @staticmethod
    def _stock_quantity(item: dict[str, Any], product: dict[str, Any] | None = None) -> int | None:
        """Return Wix's exact inventory, without treating its disabled counter as stock."""
        inventory = item.get("inventory")
        if not isinstance(inventory, dict) and product is not None:
            inventory = product.get("inventory")
        if not isinstance(inventory, dict):
            inventory = {}

        in_stock = item.get("isInStock", item.get("inStock"))
        status = str(inventory.get("status") or "").lower()
        if in_stock is False or status == "out_of_stock":
            return 0

        tracking = item.get("isTrackingInventory")
        if tracking is None and product is not None:
            tracking = product.get("isTrackingInventory")
        quantity = inventory.get("quantity")
        if tracking is True and isinstance(quantity, int) and not isinstance(quantity, bool) and quantity >= 0:
            return quantity
        return None

    @staticmethod
    def _compare_price(variant: dict[str, Any]) -> float | None:
        value = variant.get("comparePrice")
        if isinstance(value, (int, float)) and value and value != variant.get("price"):
            return float(value)
        return None

    @staticmethod
    def _images(product: dict[str, Any]) -> list[str]:
        found = []
        for media in product.get("media") or []:
            if isinstance(media, dict) and media.get("fullUrl"):
                found.append(domain.clean(media["fullUrl"]))
        return list(dict.fromkeys(found))

    @staticmethod
    def _currency(document: str) -> str | None:
        match = re.search(r'"currency":"([A-Z]{3})"', document)
        return match.group(1) if match else None
