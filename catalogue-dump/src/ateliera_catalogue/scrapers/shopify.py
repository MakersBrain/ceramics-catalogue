"""Shopify storefronts, collected through the public products.json feed.

Shopify publishes the whole catalogue as JSON with every variant, option and
image already structured, so no page rendering is needed. Collections are read
first when the source declares a materials allowlist, which keeps the request
count proportional to the part of the shop we actually want.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from . import domain
from . import record as record_module
from .base import Blocked, Scraper

PAGE_SIZE = 250


class ShopifyScraper(Scraper):
    platform = "shopify"
    method = "api_json"
    currency: str | None = None

    async def scrape(self, limit: int | None = None) -> Any:
        await self._resolve_currency()
        collections = self.config.get("collections") or []
        if collections:
            await self._scrape_collections(collections, limit)
        else:
            await self._scrape_all(limit)
        return self.result

    async def _resolve_currency(self) -> None:
        """products.json omits the currency, so read the shop's own meta.json."""
        self.currency = self.config.get("currency")
        if self.currency:
            return
        try:
            payload = await self.fetcher.json(f"{self.origin()}/meta.json")
            self.result.requests += 1
            self.currency = domain.clean(payload.get("currency")) or None
        except (httpx.HTTPError, Blocked, AttributeError) as error:
            self.note(f"shop currency unavailable from meta.json ({error})")
            self.currency = None

    async def _scrape_collections(self, collections: list[str], limit: int | None) -> None:
        for handle in collections:
            endpoint = f"{self.origin()}/collections/{handle}/products.json"
            await self._paginate(endpoint, limit, collection=handle)

    async def _scrape_all(self, limit: int | None) -> None:
        await self._paginate(f"{self.origin()}/products.json", limit)

    async def _paginate(self, endpoint: str, limit: int | None, collection: str | None = None) -> None:
        page = 1
        seen = 0
        max_pages = self.config.get("page_limit", 200)
        while page <= max_pages:
            try:
                payload = await self.fetcher.json(endpoint, params={"limit": PAGE_SIZE, "page": page})
                self.result.requests += 1
            except (httpx.HTTPError, Blocked) as error:
                self.fail(f"{endpoint}?page={page}", error)
                return
            products = payload.get("products") if isinstance(payload, dict) else None
            if not products:
                return
            self.result.discovered += len(products)
            for product in products:
                self._emit(product, collection)
                seen += 1
                if limit is not None and seen >= limit:
                    self.result.truncated = True
                    return
            if len(products) < PAGE_SIZE:
                return
            page += 1
        self.result.truncated = True

    def _emit(self, product: dict[str, Any], collection: str | None) -> None:
        handle = product.get("handle")
        if not handle:
            return
        product_url = f"{self.origin()}/products/{handle}"
        description = domain.clean(product.get("body_html"))
        product_type = domain.clean(product.get("product_type"))
        tags = product.get("tags") or []
        tags = tags if isinstance(tags, list) else [domain.clean(tags)]
        category_path = [value for value in ([collection] if collection else []) + [product_type] if value]
        category_match = self.category_allows(product_type, " ".join(tags), collection or "", handle)

        images = [
            image.get("src") for image in product.get("images") or []
            if isinstance(image, dict) and image.get("src")
        ]
        documents = domain.documents(
            [(match, match) for match in re.findall(r'href=["\']([^"\']+\.pdf[^"\']*)', product.get("body_html") or "", re.I)],
            product_url,
        )
        variants = product.get("variants") or []
        for variant in variants:
            if not isinstance(variant, dict) or (variant.get("available") is None and variant.get("price") is None):
                continue
            price, currency = record_module.parse_price(variant.get("price"))
            if price is None:
                continue
            compare_at, _ = record_module.parse_price(variant.get("compare_at_price"))
            variant_title = domain.clean(variant.get("title"))
            variant_title = "" if variant_title.casefold() == "default title" else variant_title
            name = f"{domain.clean(product.get('title'))} {variant_title}".strip()
            weight = self._weight(variant)
            row = record_module.build(
                source=self.name,
                product_url=product_url,
                variant_id=str(variant.get("id") or ""),
                name=name,
                product_name=product.get("title"),
                variant_title=variant_title or None,
                brand=product.get("vendor"),
                manufacturer_sku=self._manufacturer_sku(product, variant, variant_title),
                supplier_reference=domain.clean(variant.get("sku")) or None,
                description=description,
                category_path=category_path or None,
                image_url=(variant.get("featured_image") or {}).get("src") if isinstance(variant.get("featured_image"), dict) else (images[0] if images else None),
                all_image_urls=images or None,
                price=price,
                currency=currency or self.currency,
                price_text=f"{variant.get('price')} {currency or self.currency or ''}".strip(),
                list_price=compare_at if compare_at and compare_at != price else None,
                vat=self.config.get("vat_status"),
                availability=(
                    "https://schema.org/InStock" if variant.get("available")
                    else "https://schema.org/OutOfStock" if variant.get("available") is False
                    else None
                ),
                gtin=domain.clean(variant.get("barcode")) or None,
                technical_attributes=self._options(product, variant) | weight,
                documents=documents or None,
                extraction_method=self.method,
                source_detail_level="api",
                source_updated_at=product.get("updated_at"),
                raw={"product": {k: v for k, v in product.items() if k != "variants"}, "variant": variant},
            )
            self.add(row, category_match)

    @staticmethod
    def _weight(variant: dict[str, Any]) -> dict[str, Any]:
        grams = variant.get("grams")
        return {"shipping_weight_g": grams} if isinstance(grams, (int, float)) and grams else {}

    @staticmethod
    def _options(product: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
        names = [
            domain.clean(option.get("name"))
            for option in product.get("options") or []
            if isinstance(option, dict)
        ]
        values = [variant.get(f"option{index}") for index in (1, 2, 3)]
        return {
            name: domain.clean(value)
            for name, value in zip(names, values)
            if name and value and domain.clean(value).casefold() != "default title"
        }

    def _manufacturer_sku(
        self, product: dict[str, Any], variant: dict[str, Any], variant_title: str = "",
    ) -> str | None:
        """Record a manufacturer code only when that manufacturer is named.

        Shops often sell a whole colour range as one product whose variants are
        the colours ("Ivory Specks FN061 / 473 ml"), so the variant carries the
        code and the product title does not.
        """
        return domain.manufacturer_code(
            domain.clean(product.get("vendor")) or self.config.get("brand"),
            variant_title,
            domain.clean(variant.get("sku")),
            domain.clean(product.get("title")),
        )
