"""WooCommerce storefronts, collected through the public Store API.

/wp-json/wc/store/v1/products returns prices in minor units with attributes,
categories and variation ids, so nothing needs rendering. Variable products are
joined against a single bulk `type=variation` pass rather than one request per
product, which keeps a 2500-product shop to a few dozen requests.

Some WooCommerce sites are manufacturer catalogues rather than shops and publish
every price as zero. Those sources are marked identity_only and emit identity
records that carry specifications without inventing an offer.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from . import domain
from . import record as record_module
from .base import Blocked, Scraper

PAGE_SIZE = 100


class WooCommerceScraper(Scraper):
    platform = "woocommerce"
    method = "api_json"

    def store_api(self, path: str = "products") -> str:
        return f"{self.origin()}/wp-json/wc/store/v1/{path}"

    @property
    def identity_only(self) -> bool:
        return bool(self.config.get("identity_only"))

    async def scrape(self, limit: int | None = None) -> Any:
        categories = await self._resolve_categories()
        products: list[dict[str, Any]] = []
        if categories:
            for slug, identifier in categories.items():
                products.extend(await self._collect({"category": identifier}, limit, slug))
        else:
            products.extend(await self._collect({}, limit, None))

        unique: dict[Any, dict[str, Any]] = {}
        for product in products:
            unique.setdefault(product["id"], product)
        products = list(unique.values())
        self.result.discovered = len(products)

        variations = await self._collect_variations(products)
        for product in products:
            self._emit(product, variations.get(product["id"], []))
        return self.result

    async def _resolve_categories(self) -> dict[str, Any]:
        """Map configured materials category slugs onto Store API ids."""
        wanted = self.config.get("store_categories") or []
        if not wanted:
            return {}
        endpoint = self.store_api("products/categories")
        try:
            payload = await self.fetcher.json(endpoint, params={"per_page": PAGE_SIZE})
            self.result.requests += 1
        except (httpx.HTTPError, Blocked) as error:
            self.fail(endpoint, error)
            return {}
        available = {
            domain.clean(entry.get("slug")): entry.get("id")
            for entry in payload
            if isinstance(entry, dict) and entry.get("slug")
        }
        resolved = {slug: available[slug] for slug in wanted if slug in available}
        if missing := [slug for slug in wanted if slug not in available]:
            self.note(f"categories absent from the Store API: {', '.join(missing)}")
        return resolved

    async def _collect(self, params: dict[str, Any], limit: int | None, category: str | None) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        page = 1
        max_pages = self.config.get("page_limit", 100)
        while page <= max_pages:
            query = {"per_page": PAGE_SIZE, "page": page, **params}
            try:
                payload = await self.fetcher.json(self.store_api(), params=query)
                self.result.requests += 1
            except httpx.HTTPStatusError as error:
                if error.response.status_code == 400 and page > 1:
                    # Woo answers 400 past the last page; that is the end of the
                    # catalogue, not a hole in it, so it stays a plain break.
                    break
                self.enumeration_failed(f"{self.store_api()} page={page}", error)
                break
            except (httpx.HTTPError, Blocked) as error:
                self.enumeration_failed(f"{self.store_api()} page={page}", error)
                break
            if not isinstance(payload, list) or not payload:
                break
            for product in payload:
                if isinstance(product, dict) and product.get("id"):
                    product["_category_slug"] = category
                    collected.append(product)
            if limit is not None and len(collected) >= limit:
                self.result.truncated = True
                return collected[:limit]
            if len(payload) < PAGE_SIZE:
                break
            page += 1
        else:
            self.result.truncated = True
        return collected

    async def _collect_variations(self, products: list[dict[str, Any]]) -> dict[Any, list[dict[str, Any]]]:
        """One bulk pass over every variation, grouped by parent product id."""
        wanted = {product["id"] for product in products if product.get("type") == "variable"}
        if not wanted:
            return {}
        grouped: dict[Any, list[dict[str, Any]]] = {}
        page = 1
        max_pages = self.config.get("variation_page_limit", 200)
        while page <= max_pages:
            try:
                payload = await self.fetcher.json(
                    self.store_api(), params={"per_page": PAGE_SIZE, "page": page, "type": "variation"},
                )
                self.result.requests += 1
            except httpx.HTTPStatusError as error:
                if error.response.status_code == 400 and page > 1:
                    break
                self.fail(f"{self.store_api()} variations page={page}", error)
                break
            except (httpx.HTTPError, Blocked) as error:
                self.fail(f"{self.store_api()} variations page={page}", error)
                break
            if not isinstance(payload, list) or not payload:
                break
            for variation in payload:
                if isinstance(variation, dict) and variation.get("parent") in wanted:
                    grouped.setdefault(variation["parent"], []).append(variation)
            if len(payload) < PAGE_SIZE:
                break
            page += 1
        missing = wanted - set(grouped)
        if missing:
            self.note(f"{len(missing)} variable products returned no variations")
        return grouped

    def _emit(self, product: dict[str, Any], variations: list[dict[str, Any]]) -> None:
        product_url = domain.clean(product.get("permalink"))
        if not product_url:
            return
        categories = [
            domain.clean(entry.get("name"))
            for entry in product.get("categories") or []
            if isinstance(entry, dict) and entry.get("name")
        ]
        slugs = [
            domain.clean(entry.get("slug"))
            for entry in product.get("categories") or []
            if isinstance(entry, dict) and entry.get("slug")
        ]
        category_path = [value for value in ([product.get("_category_slug")] if product.get("_category_slug") else []) + categories if value]
        category_match = self.category_allows(" ".join(slugs + categories), product.get("name"))
        description = domain.clean(product.get("description")) or domain.clean(product.get("short_description"))
        images = [
            domain.clean(image.get("src"))
            for image in product.get("images") or []
            if isinstance(image, dict) and image.get("src")
        ]
        attributes, attribute_claims = self._attributes(product)
        brand = self._resolved_brand(product, attributes)
        documents = domain.documents(
            [(match, match) for match in re.findall(r'href=["\']([^"\']+\.pdf[^"\']*)', product.get("description") or "", re.I)],
            product_url,
        )
        common = {
            "source": self.name,
            "parent_url": product_url,
            "brand": brand,
            "description": description,
            "category_path": category_path or None,
            "all_image_urls": images or None,
            "image_url": images[0] if images else None,
            "technical_attributes": attributes or None,
            "documents": documents or None,
            "claims": attribute_claims or None,
            "extraction_method": self.method,
            "source_detail_level": "api",
            "identity_only": self.identity_only,
            "vat_rate": self.config.get("vat_rate"),
        }

        if variations:
            for variation in variations:
                price, currency, list_price = self._prices(variation.get("prices") or {})
                if price is None and not self.identity_only:
                    continue
                variant_title = self._variant_title(variation)
                self.add(
                    record_module.build(
                        **common,
                        product_url=domain.clean(variation.get("permalink")) or product_url,
                        variant_id=str(variation.get("id")),
                        name=f"{domain.clean(product.get('name'))} {variant_title}".strip(),
                        product_name=product.get("name"),
                        variant_title=variant_title or None,
                        manufacturer_sku=self._manufacturer_sku(variant_title, variation, product, brand),
                        supplier_reference=domain.clean(variation.get("sku")) or domain.clean(product.get("sku")) or None,
                        price=price,
                        currency=currency,
                        list_price=list_price,
                        vat=self.config.get("vat_status"),
                        availability="https://schema.org/InStock" if variation.get("is_in_stock", True) else "https://schema.org/OutOfStock",
                        stock_quantity=self._stock_quantity(variation),
                        raw={"product": {k: v for k, v in product.items() if k != "variations"}, "variation": variation},
                    ),
                    category_match,
                )
            return

        price, currency, list_price = self._prices(product.get("prices") or {})
        if price is None and not self.identity_only:
            return
        self.add(
            record_module.build(
                **common,
                product_url=product_url,
                name=product.get("name"),
                manufacturer_sku=self._manufacturer_sku("", {}, product, brand),
                supplier_reference=domain.clean(product.get("sku")) or None,
                price=price,
                currency=currency,
                price_text=domain.clean(product.get("price_html")) or None,
                list_price=list_price,
                vat=self.config.get("vat_status"),
                availability="https://schema.org/InStock" if product.get("is_in_stock") else "https://schema.org/OutOfStock",
                stock_quantity=self._stock_quantity(product),
                raw=product,
            ),
            category_match,
        )

    def _stock_quantity(self, item: dict[str, Any]) -> int | None:
        """Read exact stock when a storefront exposes it as its cart ceiling.

        WooCommerce deliberately does not publish ``stock_quantity`` through
        the public Store API. It does publish ``add_to_cart.maximum``, but that
        field is only a purchasing constraint in the general case: an
        untracked item defaults to a large limit, a sold-individually item is
        capped at one, and a backordered item can have a shop-defined ceiling.

        Some storefronts do use that value as their live inventory. This is
        source-configured only after varied ceilings have been verified against
        the low-stock count whenever WooCommerce also prints one. ``9999`` is
        WooCommerce's default for untracked stock and is never inventory. No
        cart is mutated to discover any of these values.
        """
        if item.get("is_in_stock") is False:
            return 0

        stock = item.get("stock_availability")
        if isinstance(stock, dict) and isinstance(stock.get("quantity"), int):
            return stock["quantity"]

        low = item.get("low_stock_remaining")
        if isinstance(low, int) and low > 0:
            return low

        if not self.config.get("stock_from_add_to_cart_maximum"):
            return None
        if item.get("is_in_stock") is not True or item.get("is_on_backorder") or item.get("sold_individually"):
            return None
        add_to_cart = item.get("add_to_cart")
        maximum = add_to_cart.get("maximum") if isinstance(add_to_cart, dict) else None
        return maximum if isinstance(maximum, int) and 0 < maximum < 9999 else None

    @staticmethod
    def _variant_title(variation: dict[str, Any]) -> str:
        if title := domain.clean(variation.get("variation")):
            return title
        return ", ".join(
            domain.clean(entry.get("value"))
            for entry in variation.get("attributes") or []
            if isinstance(entry, dict) and entry.get("value")
        )

    def _attributes(self, product: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Read Store API attributes, and lift safety attributes into claims.

        Mayco publishes dinnerware safety as an attribute whose only term is an
        icon URL, so the claim has to be read from the file name.
        """
        attributes: dict[str, Any] = {}
        claims: list[dict[str, Any]] = []
        for attribute in product.get("attributes") or []:
            if not isinstance(attribute, dict) or not attribute.get("name"):
                continue
            name = domain.clean(attribute.get("name"))
            terms = [domain.clean(term.get("name")) for term in attribute.get("terms") or [] if isinstance(term, dict)]
            if not terms:
                continue
            value = ", ".join(terms)
            attributes[name] = value
            if re.search(r"dinnerware|food", name, re.I):
                positive = not re.search(r"\bnot[-\s]", value, re.I)
                claims.append({
                    "type": "food_contact_suitability",
                    "claim": positive,
                    "evidence": f"{name}: {value}"[:300],
                    "basis": "product_attribute",
                })
        return attributes, claims

    BRAND_ATTRIBUTES = ("brand", "marque", "marke", "merk", "marca", "marque email", "gamintojas")

    @classmethod
    def _brand(cls, product: dict[str, Any], attributes: dict[str, Any] | None = None) -> str | None:
        value = product.get("brands")
        if isinstance(value, list) and value and isinstance(value[0], dict):
            if name := domain.clean(value[0].get("name")):
                return name
        for key, term in (attributes or {}).items():
            if domain.fold(key) in cls.BRAND_ATTRIBUTES and domain.clean(term):
                return domain.clean(term)
        return None

    def _resolved_brand(self, product: dict[str, Any], attributes: dict[str, Any]) -> str | None:
        """A single-manufacturer catalogue can declare its brand in the config."""
        return self._brand(product, attributes) or self.config.get("brand")

    @staticmethod
    def _manufacturer_sku(variant_title: str, variation: dict[str, Any], product: dict[str, Any], brand: str | None) -> str | None:
        """Record a manufacturer code only when that manufacturer is actually named."""
        return domain.manufacturer_code(
            brand,
            domain.clean(product.get("name")),
            variant_title,
            domain.clean(product.get("sku")),
            domain.clean(variation.get("sku")),
        )

    @staticmethod
    def _prices(prices: dict[str, Any]) -> tuple[float | None, str | None, float | None]:
        """Read the Store API money object, honouring its minor-unit scaling."""
        if not isinstance(prices, dict):
            return None, None, None
        try:
            divisor = 10 ** int(prices.get("currency_minor_unit", 2))
        except (TypeError, ValueError):
            divisor = 100

        def amount(value: Any) -> float | None:
            try:
                return int(str(value)) / divisor
            except (TypeError, ValueError):
                return None

        price = amount(prices.get("price"))
        regular = amount(prices.get("regular_price"))
        currency = domain.clean(prices.get("currency_code")) or None
        return price, currency, regular if regular and regular != price else None
