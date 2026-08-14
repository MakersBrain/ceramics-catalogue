"""SumUp Online Store storefronts (`*.sumupstore.com` and custom domains).

SumUp renders its shops with the Next.js App Router. The product listing is
built client-side from an API that robots.txt disallows, but every product page
ships the complete product — variants, per-variant price and the exact tracked
inventory — in the React Server Components payload the page streams to itself
through `self.__next_f.push`. That payload is what this scraper reads, and the
product URLs come from the store's own `sitemap.products.xml`.

The routes are localised (`/article/<slug>` on a French shop, `/product/<slug>`
on an English one), so nothing here matches on the path; the sitemap is the
authority on what a product URL looks like for a given shop.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import unquote, urlparse

from . import domain, jsonld
from . import record as record_module
from .pagecrawl import PageScraper, canonical

#: One `self.__next_f.push([1, "<chunk>"])` call. The payload is split across
#: dozens of them, in document order, and only the string chunks carry data.
FLIGHT_CHUNK = re.compile(r"self\.__next_f\.push\((\[.*?\])\)</script>", re.S)
PRODUCT_MARKER = re.compile(r'"product":\{"id":"[0-9a-f-]{8}-')
CURRENCY = re.compile(r'"currency":"([A-Z]{3})"')


def flight_payload(document: str) -> str:
    """Reassemble the RSC stream a Next.js page pushes to itself."""
    parts = []
    for raw in FLIGHT_CHUNK.findall(document):
        try:
            chunk = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if len(chunk) > 1 and isinstance(chunk[1], str):
            parts.append(chunk[1])
    return "".join(parts)


class SumUpScraper(PageScraper):
    platform = "sumup"
    method = "dom"

    async def discover_from_sitemaps(self) -> list[str]:
        if self.config.get("sitemaps"):
            return await super().discover_from_sitemaps()
        # The advertised sitemap is an index over products *and* categories, and
        # a category URL is indistinguishable from a product one by path alone.
        # Naming the product sub-sitemap is what keeps categories out.
        found = await self.sitemap_urls([f"{self.origin()}/sitemap.products.xml"])
        urls = [canonical(url) for url in found if self.is_product_url(url)]
        if urls:
            return urls
        return await super().discover_from_sitemaps()

    def product_of(self, payload: str, url: str) -> dict[str, Any] | None:
        """Find the product this page is about, not the ones it links to.

        A product page carries several product objects: the one being shown and
        the related items beside it. A related item is the *listing* shape —
        `variants` present but empty — and the shop's home and product-list
        pages are nothing but that shape. Both are why the slug has to agree
        before an object is accepted: the store's own products sitemap lists the
        home page beside the products, and taking the first object on it
        published a suggested product's price under the shop's root URL.

        The fallback for a route this scraper has not seen (the path segment is
        localised) accepts only an object that actually carries variant detail,
        which no listing-shaped one does.
        """
        slug = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
        candidates = []
        for match in PRODUCT_MARKER.finditer(payload):
            product = jsonld.balanced_object(payload, match.start() + len('"product":'))
            if product is None:
                continue
            if slug and product.get("slug") == slug:
                return product
            candidates.append(product)
        detailed = [product for product in candidates if self._variant_detail(product)]
        if len(detailed) == 1:
            return detailed[0]
        return None

    @staticmethod
    def _variant_detail(product: dict[str, Any]) -> int:
        variants = product.get("variants")
        if not isinstance(variants, dict):
            return 0
        return sum(1 for variant in variants.values() if isinstance(variant, dict) and variant)

    def parse(self, document: str, url: str) -> list[tuple[dict[str, Any], bool | None]]:
        payload = flight_payload(document)
        product = self.product_of(payload, url) if payload else None
        if product is None:
            return super().parse(document, url)

        name = domain.clean(product.get("name")) or jsonld.meta(document, "og:title")
        if not name:
            return []
        description = domain.clean(product.get("description"))
        images = [
            domain.clean(image)
            for image in (product.get("allImages") or [product.get("image")])
            if domain.clean(image)
        ]
        images = list(dict.fromkeys(images))
        category = domain.clean((product.get("category") or {}).get("name"))
        categories = [category] if category else []
        currency = self._currency(payload)
        brand = self.config.get("brand")

        common = dict(
            source=self.name,
            product_url=canonical(url),
            parent_url=canonical(url),
            product_name=name,
            brand=brand,
            description=description or None,
            category_path=categories or None,
            image_url=images[0] if images else None,
            all_image_urls=images or None,
            currency=currency or self.config.get("currency"),
            vat=self.config.get("vat_status"),
            vat_rate=self.config.get("vat_rate"),
            extraction_method=self.method,
            source_detail_level="product_page",
        )

        rows = []
        for variant in self._variants(product):
            price = self._amount(variant.get("price"))
            if price is None:
                continue
            title = domain.clean(variant.get("name")) or None
            options = self._options(variant)
            rows.append((
                record_module.build(
                    **common,
                    variant_id=str(variant.get("uuid") or "") or None,
                    variant_title=title,
                    name=f"{name} {title}".strip() if title else name,
                    supplier_reference=domain.clean(variant.get("sku") or product.get("sku")) or None,
                    price=price,
                    list_price=self._list_price(variant),
                    availability=(
                        "https://schema.org/InStock" if variant.get("isAvailable", True)
                        else "https://schema.org/OutOfStock"
                    ),
                    stock_quantity=self._stock_quantity(variant, product),
                    technical_attributes=options or None,
                    raw={"product": {key: value for key, value in product.items() if key != "variants"},
                         "variant": variant},
                ),
                self.category_allows(category or "", name),
            ))
        return rows

    def _variants(self, product: dict[str, Any]) -> list[dict[str, Any]]:
        """The purchasable variants, falling back to the product itself.

        SumUp gives an unvaried product exactly one variant, so there is no
        placeholder to collapse — but a listing-shaped object has variant keys
        mapping to empty dicts, and those still price at the product level.
        """
        variants = product.get("variants")
        found = []
        if isinstance(variants, dict):
            for uuid, variant in variants.items():
                if not isinstance(variant, dict):
                    continue
                found.append({
                    "uuid": variant.get("uuid") or uuid,
                    "name": variant.get("name"),
                    "sku": variant.get("sku"),
                    "price": variant.get("price", product.get("price")),
                    "basePrice": variant.get("basePrice", product.get("basePrice")),
                    "hasDiscount": variant.get("hasDiscount", product.get("hasDiscount")),
                    "options": variant.get("options"),
                    "quantity": variant.get("quantity"),
                    "isAvailable": variant.get("isAvailable", product.get("isAvailable", True)),
                    "isTrackingEnabled": variant.get("isTrackingEnabled"),
                })
        if found:
            return found
        return [{
            "uuid": None,
            "name": None,
            "sku": product.get("sku"),
            "price": product.get("price"),
            "basePrice": product.get("basePrice"),
            "hasDiscount": product.get("hasDiscount"),
            "options": None,
            "quantity": None,
            "isAvailable": product.get("isAvailable", True),
            "isTrackingEnabled": product.get("isTrackingEnabled"),
        }]

    @staticmethod
    def _amount(value: Any) -> float | None:
        """SumUp quotes money in minor units; 2500 is 25.00."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return round(float(value) / 100, 2)

    def _list_price(self, variant: dict[str, Any]) -> float | None:
        """The pre-discount price, only where the shop says there is a discount."""
        if not variant.get("hasDiscount"):
            return None
        base = self._amount(variant.get("basePrice"))
        if base is None or base == self._amount(variant.get("price")):
            return None
        return base

    @staticmethod
    def _stock_quantity(variant: dict[str, Any], product: dict[str, Any]) -> int | None:
        """The exact inventory, but only where the shop is counting it.

        `quantity` is present on every variant. It means something only when
        inventory tracking is on: with tracking off it is the shop's own
        untracked placeholder, and reporting it would invent a stock ceiling for
        a product that has none.
        """
        if variant.get("isAvailable") is False:
            return 0
        tracking = variant.get("isTrackingEnabled")
        if tracking is None:
            tracking = product.get("isTrackingEnabled")
        quantity = variant.get("quantity")
        if tracking is True and isinstance(quantity, int) and not isinstance(quantity, bool) and quantity >= 0:
            return quantity
        return None

    @staticmethod
    def _options(variant: dict[str, Any]) -> dict[str, Any]:
        """Variant options as an attribute mapping, whatever shape they arrive in."""
        options = variant.get("options")
        if not isinstance(options, list):
            return {}
        attributes: dict[str, Any] = {}
        for index, option in enumerate(options):
            if isinstance(option, dict):
                key = domain.clean(option.get("name") or option.get("label"))
                value = domain.clean(option.get("value") or option.get("choice"))
                if key and value:
                    attributes[key] = value
            elif isinstance(option, str) and domain.clean(option):
                attributes[f"option_{index + 1}"] = domain.clean(option)
        return attributes

    @staticmethod
    def _currency(payload: str) -> str | None:
        match = CURRENCY.search(payload)
        return match.group(1) if match else None
