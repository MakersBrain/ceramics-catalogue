"""BigCommerce storefronts, collected through their public GraphQL API.

Every BigCommerce theme embeds a storefront API token in the page it serves to
anonymous visitors; that token is what the shop's own JavaScript uses. It is
read from the public page, used for the session and never stored.

The API returns variants, option values, inventory and custom fields in one
request per 50 products, which is both far cheaper and far richer than parsing
rendered category pages.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

import httpx

from . import domain
from . import record as record_module
from .base import Blocked, Scraper

#: Themes expose the token as JSON ("storefront_api_token":"...") or as a script
#: assignment (window.storefront_token = "..."). Both shapes are collected and
#: then filtered by what the token itself says it is valid for.
TOKEN_PATTERN = r'(?:storefront_api_token|storefront_token|local_token|storefrontApiToken)\\?["\']?\s*[:=]\s*\\?["\']([A-Za-z0-9._-]{40,})'

CATALOGUE_QUERY = """
query Catalogue($after: String) {
  site {
    products(first: 50, after: $after) {
      pageInfo { hasNextPage endCursor }
      edges { node {
        entityId name path sku description
        brand { name }
        availabilityV2 { status description }
        defaultImage { urlOriginal }
        images(first: 12) { edges { node { urlOriginal } } }
        prices { price { value currencyCode } retailPrice { value } }
        categories { edges { node { name path } } }
        customFields { edges { node { name value } } }
        variants(first: 30) { edges { node {
          entityId sku
          defaultImage { urlOriginal }
          prices { price { value currencyCode } }
          inventory { isInStock aggregated { availableToSell } }
          options { edges { node {
            displayName values { edges { node { label } } }
          } } }
        } } }
      } }
    }
  }
}
"""


def _edges(container: Any, *path: str) -> list[dict[str, Any]]:
    """Walk a GraphQL connection and return its nodes."""
    for key in path:
        container = (container or {}).get(key) or {}
    return [edge.get("node") or {} for edge in (container.get("edges") or []) if isinstance(edge, dict)]


class BigCommerceScraper(Scraper):
    platform = "bigcommerce"
    method = "graphql"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.token: str | None = None
        #: Set once the storefront proves it rejects plain HTTP clients.
        self.via_browser = False

    @property
    def token_page(self) -> str:
        return self.config.get("category_url") or self.base_url

    async def scrape(self, limit: int | None = None) -> Any:
        self.token = await self._discover_token()
        if not self.token:
            raise Blocked("no BigCommerce storefront token found on the public pages")
        cursor: str | None = None
        seen = 0
        for _ in range(self.config.get("page_limit", 200)):
            data = await self._graphql({"after": cursor})
            connection = ((data.get("site") or {}).get("products")) or {}
            nodes = [edge.get("node") or {} for edge in connection.get("edges") or []]
            if not nodes:
                break
            self.result.discovered += len(nodes)
            for product in nodes:
                self._emit(product)
                seen += 1
                if limit is not None and seen >= limit:
                    self.result.truncated = True
                    return self.result
            page_info = connection.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                return self.result
            cursor = page_info.get("endCursor")
        self.result.truncated = True
        return self.result

    async def _discover_token(self) -> str | None:
        for page in [value for value in (self.config.get("category_url"), self.base_url) if value]:
            document = None
            try:
                document = await self.fetcher.text(page, browser_user_agent=True)
                self.result.requests += 1
            except (httpx.HTTPError, Blocked) as error:
                # A storefront behind TLS fingerprinting rejects every Python
                # client but serves the same public page to a real browser.
                self.note(f"plain HTTP refused by {page} ({error}); using the browser transport")
                try:
                    document = await self.fetcher.render(page, wait_ms=2500)
                    self.result.rendered_pages += 1
                    self.via_browser = True
                except Blocked as browser_error:
                    self.fail(page, browser_error)
                    continue
            for candidate in dict.fromkeys(re.findall(TOKEN_PATTERN, document)):
                if self._token_allows_origin(candidate):
                    self.note(f"storefront token discovered on {page}")
                    return candidate
            if re.search(TOKEN_PATTERN, document):
                self.note(f"tokens on {page} are all scoped to another origin")
        return None

    def _token_allows_origin(self, token: str) -> bool:
        """Keep only a token the storefront itself says is valid for this origin.

        BigCommerce themes also ship a development token whose CORS claim is
        localhost, which the API rejects with an origin error.
        """
        parts = token.split(".")
        if len(parts) < 2:
            return False
        try:
            padded = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(padded))
        except (ValueError, json.JSONDecodeError):
            return False
        allowed = claims.get("cors")
        if not allowed:
            return True
        return any(domain.clean(entry).rstrip("/") == self.origin() for entry in allowed)

    async def _graphql(self, variables: dict[str, Any]) -> dict[str, Any]:
        endpoint = f"{self.origin()}/graphql"
        body = {"query": CATALOGUE_QUERY, "variables": variables}
        headers = {
            "authorization": f"Bearer {self.token}",
            "content-type": "application/json",
        }
        if self.via_browser:
            payload = await self.fetcher.request_json_in_browser(
                self.token_page, endpoint, headers=headers, body=body,
            )
            self.result.requests += 1
        else:
            response = await self.fetcher.response(
                endpoint,
                method="POST",
                json_body=body,
                headers={**headers, "origin": self.origin(), "referer": self.token_page},
                browser_user_agent=True,
            )
            self.result.requests += 1
            payload = response.json()
        if payload.get("errors"):
            raise Blocked(f"GraphQL error: {payload['errors']}")
        return payload.get("data") or {}

    def _emit(self, product: dict[str, Any]) -> None:
        path = domain.clean(product.get("path"))
        if not path:
            return
        product_url = f"{self.origin()}{path}" if path.startswith("/") else path
        categories = [domain.clean(node.get("name")) for node in _edges(product, "categories") if node.get("name")]
        category_match = self.category_allows(" ".join(categories), product.get("name"))
        brand = domain.clean((product.get("brand") or {}).get("name")) or self.config.get("brand")
        description = domain.clean(product.get("description"))
        images = [
            domain.clean(node.get("urlOriginal"))
            for node in _edges(product, "images") if node.get("urlOriginal")
        ]
        default_image = domain.clean((product.get("defaultImage") or {}).get("urlOriginal"))
        custom_fields = {
            domain.clean(node.get("name")): domain.clean(node.get("value"))
            for node in _edges(product, "customFields")
            if node.get("name") and node.get("value")
        }
        documents = domain.documents(
            [(value, name) for name, value in custom_fields.items() if ".pdf" in value.lower()],
            product_url,
        )
        availability = (product.get("availabilityV2") or {}).get("status")
        parent_price = ((product.get("prices") or {}).get("price") or {})
        retail = ((product.get("prices") or {}).get("retailPrice") or {})

        common = {
            "source": self.name,
            "parent_url": product_url,
            "product_url": product_url,
            "brand": brand,
            "description": description,
            "category_path": categories or None,
            "all_image_urls": images or ([default_image] if default_image else None),
            "documents": documents or None,
            "extraction_method": self.method,
            "source_detail_level": "api",
            "vat": self.config.get("vat_status"),
        }

        variants = _edges(product, "variants")
        if not variants:
            price = parent_price.get("value")
            if price is None:
                return
            self.add(
                record_module.build(
                    **common,
                    name=product.get("name"),
                    manufacturer_sku=domain.manufacturer_code(brand, domain.clean(product.get("name")), domain.clean(product.get("sku"))),
                    supplier_reference=domain.clean(product.get("sku")) or None,
                    image_url=default_image or (images[0] if images else None),
                    price=float(price),
                    currency=domain.clean(parent_price.get("currencyCode")) or None,
                    list_price=float(retail["value"]) if retail.get("value") not in (None, price) else None,
                    availability=self._availability(availability),
                    technical_attributes=custom_fields or None,
                    raw=product,
                ),
                category_match,
            )
            return

        for variant in variants:
            variant_price = ((variant.get("prices") or {}).get("price") or {})
            price = variant_price.get("value", parent_price.get("value"))
            if price is None:
                continue
            options = {
                domain.clean(node.get("displayName")): ", ".join(
                    domain.clean(value.get("label")) for value in _edges(node, "values") if value.get("label")
                )
                for node in _edges(variant, "options") if node.get("displayName")
            }
            variant_title = ", ".join(value for value in options.values() if value)
            inventory = variant.get("inventory") or {}
            aggregated = inventory.get("aggregated") or {}
            variant_image = domain.clean((variant.get("defaultImage") or {}).get("urlOriginal"))
            self.add(
                record_module.build(
                    **common,
                    variant_id=str(variant.get("entityId") or ""),
                    name=f"{domain.clean(product.get('name'))} {variant_title}".strip(),
                    product_name=product.get("name"),
                    variant_title=variant_title or None,
                    manufacturer_sku=domain.manufacturer_code(
                        brand, domain.clean(product.get("name")), domain.clean(variant.get("sku")),
                    ),
                    supplier_reference=domain.clean(variant.get("sku")) or domain.clean(product.get("sku")) or None,
                    image_url=variant_image or default_image or (images[0] if images else None),
                    price=float(price),
                    currency=domain.clean(variant_price.get("currencyCode") or parent_price.get("currencyCode")) or None,
                    availability=(
                        "https://schema.org/InStock" if inventory.get("isInStock")
                        else "https://schema.org/OutOfStock" if inventory.get("isInStock") is False
                        else self._availability(availability)
                    ),
                    stock_quantity=aggregated.get("availableToSell"),
                    technical_attributes=(custom_fields | options) or None,
                    raw={"product": {k: v for k, v in product.items() if k != "variants"}, "variant": variant},
                ),
                category_match,
            )

    @staticmethod
    def _availability(status: Any) -> str | None:
        mapping = {
            "Available": "https://schema.org/InStock",
            "Preorder": "https://schema.org/PreOrder",
            "Unavailable": "https://schema.org/OutOfStock",
        }
        return mapping.get(domain.clean(status))
