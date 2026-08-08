"""Supplier adapter profiles and platform-aware product extraction."""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse


def _clean(value: Any) -> str:
    value = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _jsonld(document: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    scripts = re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', document, re.I | re.S)
    def flatten(value: Any) -> None:
        if isinstance(value, list):
            for child in value: flatten(child)
        elif isinstance(value, dict) and "@graph" in value:
            flatten(value["@graph"])
        elif isinstance(value, dict): result.append(value)
    for raw in scripts:
        try: flatten(json.loads(html.unescape(raw)))
        except (json.JSONDecodeError, TypeError): continue
    return result


def _meta(document: str, key: str) -> str | None:
    escaped = re.escape(key)
    patterns = [
        rf'<meta[^>]+(?:property|name)=["\']{escaped}["\'][^>]+content=["\']([^"\']*)',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']{escaped}["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, document, re.I)
        if match: return html.unescape(match.group(1)).strip()
    return None


def _data_attribute(document: str, element_id: str, attribute: str) -> dict[str, Any] | None:
    """Decode a JSON-valued HTML attribute from a named element."""
    identifier = re.escape(element_id)
    name = re.escape(attribute)
    patterns = (
        rf'<[^>]+\bid=["\']{identifier}["\'][^>]+\b{name}=["\']([^"\']+)["\']',
        rf'<[^>]+\b{name}=["\']([^"\']+)["\'][^>]+\bid=["\']{identifier}["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, document, re.I | re.S)
        if not match:
            continue
        try:
            value = json.loads(html.unescape(match.group(1)))
        except (json.JSONDecodeError, TypeError):
            continue
        return value if isinstance(value, dict) else None
    return None


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    return {
        "transparencia": "transparency",
        "efectos": "effects",
        "weight_volume": "package_size",
    }.get(value, value)


def _firing_range(value: str) -> dict[str, Any] | None:
    text = _clean(value)
    patterns = (
        r"(?P<minimum>[0-9]{3,4})\s*(?:[º°]\s*C)?\s*[-–—]\s*(?P<maximum>[0-9]{3,4})\s*[º°]?\s*C",
        r"between\s+(?P<minimum>[0-9]{3,4})\s*[º°]?\s*C\s+and\s+(?P<maximum>[0-9]{3,4})\s*[º°]?\s*C",
    )
    for pattern in patterns:
        if match := re.search(pattern, text, re.I):
            return {
                "minimum_celsius": int(match.group("minimum")),
                "maximum_celsius": int(match.group("maximum")),
                "evidence": match.group(0),
                "basis": "product_description",
            }
    return None


def _manufacturer_sku(brand: str, name: str, product_url: str, supplier_reference: str) -> str:
    """Prefer an explicit manufacturer code over a retailer's internal reference."""
    if brand.casefold().replace("®", "") != "mayco":
        return supplier_reference
    candidates = (_clean(name), urlparse(product_url).path.rsplit("/", 1)[-1].replace("-", " "))
    for candidate in candidates:
        match = re.search(
            r"\b(NTBR|NTCLR|PBDIP|[A-Z]{1,4}-?[0-9]{1,4})\b",
            candidate,
            re.I,
        )
        if match:
            return match.group(1).upper().replace("-", "")
    return supplier_reference


@dataclass(frozen=True)
class SupplierAdapter:
    name: str
    platform: str
    browser: str = "fallback"  # never, fallback, always

    def is_product_url(self, url: str, source: dict[str, Any]) -> bool:
        patterns = source.get("product_paths", [])
        return not patterns or any(re.search(pattern, urlparse(url).path) for pattern in patterns)

    def extract_items(self, document: str, page_url: str) -> list[dict[str, Any]]:
        products = []
        for item in _jsonld(document):
            types = item.get("@type", [])
            types = types if isinstance(types, list) else [types]
            if any(str(value).lower() == "product" for value in types):
                products.append(item)
        if products:
            if self.platform == "wix":
                visible = re.search(r"(?:€|EUR|USD|GBP|CHF)\s*([0-9][0-9.,]*)|([0-9][0-9.,]*)\s*(€|EUR|USD|GBP|CHF)", document, re.I)
                if visible:
                    amount = visible.group(1) or visible.group(2)
                    marker = visible.group(0)
                    currency = "USD" if "USD" in marker or "$" in marker else "GBP" if "GBP" in marker or "£" in marker else "CHF" if "CHF" in marker else "EUR"
                    for product in products:
                        offers = product.get("offers")
                        if not isinstance(offers, dict) or offers.get("price") is None:
                            product["offers"] = {"price": amount, "priceCurrency": currency}
            return products
        # Product pages without JSON-LD: OpenGraph/product meta fallback.
        name = _meta(document, "og:title")
        price = _meta(document, "product:price:amount") or _meta(document, "og:price:amount")
        currency = _meta(document, "product:price:currency") or _meta(document, "og:price:currency")
        if self.platform in {"woocommerce", "shopware"}:
            title = re.search(r'<h1[^>]*class=["\'][^"\']*(?:product[_-]title|entry-title)[^"\']*["\'][^>]*>(.*?)</h1>', document, re.I | re.S)
            price_block = re.search(r'<p[^>]*class=["\'][^"\']*\bprice\b[^"\']*["\'][^>]*>(.*?)</p>', document, re.I | re.S)
            if title: name = _clean(title.group(1))
            if not price and price_block:
                price_text = _clean(price_block.group(1))
                amount = re.search(r"[0-9][0-9.,]*", price_text)
                price = amount.group(0) if amount else None
                currency = "USD" if "$" in price_text else "GBP" if "£" in price_text else "EUR"
        if not (name and price): return []
        currency = currency or "EUR"
        sku_match = re.search(r'(?:sku|reference|référence|artikelnummer)\s*[:#]?\s*</?[^>]*>?\s*([A-Z0-9][A-Z0-9._/-]+)', document, re.I)
        return [{
            "@type": "Product", "name": name, "description": _meta(document, "og:description"),
            "image": _meta(document, "og:image"), "url": _meta(document, "og:url") or page_url,
            "sku": sku_match.group(1) if sku_match else None,
            "offers": {"price": price, "priceCurrency": currency},
        }]


class Sio2Adapter(SupplierAdapter):
    """SIO-2's PrestaShop catalogue, scoped to ceramic bodies/materials and glazes."""

    OWN_MATERIAL_CATEGORIES = frozenset({
        "low-fire-ceramic-clays",
        "high-fire-ceramic-clays",
        "specialty-ceramic-clays",
        "porcelain",
        "3d-printing-ceramic-clays",
        "modelling-clay",
        "liquid-clay",
        "other-materials",
        "raw-materials-and-oxides",
        "auxiliary-products",
    })
    GLAZE_CATEGORIES = frozenset({
        "prepared-glazes",
        "powdered-glazes",
        "prepared-underglazes",
        "powdered-underglazes",
    })

    @classmethod
    def _family(cls, category: str) -> str:
        if "underglazes" in category:
            return "underglaze"
        if category in cls.GLAZE_CATEGORIES:
            return "glaze"
        if category in {"other-materials", "raw-materials-and-oxides", "auxiliary-products"}:
            return "material"
        return "clay"

    def _listing_items(self, document: str, page_url: str) -> list[dict[str, Any]]:
        items = []
        cards = re.findall(
            r'<article[^>]+class=["\'][^"\']*\bproduct-miniature\b[^"\']*["\'][^>]*>(.*?)</article>',
            document,
            re.I | re.S,
        )
        for card in cards:
            link = re.search(
                r'<h3[^>]+class=["\'][^"\']*\bproduct-title\b[^"\']*["\'][^>]*>\s*<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                card,
                re.I | re.S,
            )
            price = re.search(
                r'<span[^>]+class=["\'][^"\']*\bproduct-price\b[^"\']*["\'][^>]+content=["\']([^"\']+)',
                card,
                re.I | re.S,
            )
            if not link or not price:
                continue
            product_url = urljoin(page_url, html.unescape(link.group(1)))
            category = urlparse(product_url).path.strip("/").split("/")[-2]
            if category not in self.OWN_MATERIAL_CATEGORIES | self.GLAZE_CATEGORIES:
                continue

            def card_text(class_name: str) -> str:
                match = re.search(
                    rf'<[^>]+class=["\'][^"\']*\b{re.escape(class_name)}\b[^"\']*["\'][^>]*>(.*?)</[^>]+>',
                    card,
                    re.I | re.S,
                )
                return _clean(match.group(1)) if match else ""

            brand = card_text("product-brand")
            if category in self.OWN_MATERIAL_CATEGORIES and brand.casefold().replace("®", "") != "sio-2":
                continue
            description = card_text("product-description-short")
            supplier_reference = card_text("product-reference")
            manufacturer_sku = _manufacturer_sku(
                brand,
                _clean(link.group(2)),
                product_url,
                supplier_reference,
            )
            image_match = re.search(r'data-full-size-image-url=["\']([^"\']+)', card, re.I)
            gtin_match = re.search(r"-([0-9]{8}|[0-9]{12,14})\.html$", urlparse(product_url).path)
            package_match = re.search(r"([0-9]+(?:[.,][0-9]+)?)\s*(kg|g|l|ml)\b", _clean(link.group(2)), re.I)
            attributes = {}
            if package_match:
                attributes["package_size"] = " ".join(package_match.groups())
            availability = "https://schema.org/OutOfStock" if re.search(
                r"out.of.stock|agotado|rupture", card, re.I
            ) else "https://schema.org/InStock"
            items.append({
                "@type": "Product",
                "name": _clean(link.group(2)),
                "description": description,
                "brand": {"@type": "Brand", "name": brand} if brand else None,
                "sku": manufacturer_sku or None,
                "supplier_reference": supplier_reference or None,
                "gtin": gtin_match.group(1) if gtin_match else None,
                "url": product_url,
                "image": html.unescape(image_match.group(1)) if image_match else None,
                "offers": {
                    "price": price.group(1),
                    "priceCurrency": "EUR",
                    "availability": availability,
                },
                "category": card_text("product-category-name") or None,
                "family": self._family(category),
                "material_kind": category,
                "firing_range": _firing_range(description),
                "technical_attributes": attributes,
                "documents": [],
                "claims": [],
                "all_image_urls": [html.unescape(image_match.group(1))] if image_match else [],
                "vat_status": "inclusive",
                "source_detail_level": "listing",
            })
        return items

    def extract_items(self, document: str, page_url: str) -> list[dict[str, Any]]:
        products = super().extract_items(document, page_url)
        details = _data_attribute(document, "product-details", "data-product")
        if not details:
            return products or self._listing_items(document, page_url)

        category = str(details.get("category") or "")
        if category not in self.OWN_MATERIAL_CATEGORIES | self.GLAZE_CATEGORIES:
            return []

        # Product/review JSON-LD snippets can coexist. Select the actual sellable item.
        product = next(
            (item for item in products if item.get("offers") and (item.get("sku") or item.get("mpn"))),
            products[0] if products else {},
        )
        features = {
            _slug(str(feature.get("name", ""))): _clean(feature.get("value"))
            for feature in details.get("features", [])
            if isinstance(feature, dict) and feature.get("name") and feature.get("value")
        }
        brand = features.get("brand") or details.get("manufacturer_name")
        jsonld_brand = product.get("brand")
        jsonld_brand = jsonld_brand.get("name") if isinstance(jsonld_brand, dict) else jsonld_brand
        brand = _clean(brand or jsonld_brand)
        if category in self.OWN_MATERIAL_CATEGORIES and brand.casefold().replace("®", "") != "sio-2":
            return []

        attachment_links = {
            match.group(2): {
                "url": urljoin(page_url, html.unescape(match.group(1))),
                "label": _clean(match.group(3)),
            }
            for match in re.finditer(
                r'<a[^>]+href=["\']([^"\']*id_attachment=([0-9]+)[^"\']*)["\'][^>]*>(.*?)</a>',
                document,
                re.I | re.S,
            )
        }
        documents = []
        for attachment in details.get("attachments", []):
            if not isinstance(attachment, dict):
                continue
            attachment_id = str(attachment.get("id_attachment") or "")
            label = _clean(attachment.get("file_name") or attachment.get("name"))
            lowered = label.casefold()
            document_type = (
                "safety_data_sheet" if re.search(r"(?:^|[^a-z])(?:msds|sds|safety)", lowered)
                else "technical_sheet" if re.search(r"technical|technique|t[eé]cnica", lowered)
                else "certificate" if re.search(r"certificat", lowered)
                else "lab_report" if re.search(r"(?:lab|laboratory).*(?:report|result)", lowered)
                else "other"
            )
            link = attachment_links.get(attachment_id, {})
            documents.append({
                "type": document_type,
                "name": label or link.get("label"),
                "url": link.get("url"),
                "mime_type": attachment.get("mime"),
            })

        images = []
        for image in details.get("images", []):
            if not isinstance(image, dict):
                continue
            candidate = image.get("large", {}).get("url")
            if candidate and candidate not in images:
                images.append(candidate)

        description = _clean(details.get("description") or product.get("description"))
        claims = []
        claim_patterns = (
            ("food_contact_suitability", r"[^.!?]*(?:food[- ]safe|suitable for tableware)[^.!?]*[.!?]?"),
            ("non_toxic", r"[^.!?]*(?:non[- ]toxic|free of toxic additives)[^.!?]*[.!?]?"),
            ("standard_conformity", r"[^.!?]*conforms? to ASTM D[- ]?4236[^.!?]*[.!?]?"),
            ("certification_mark", r"[^.!?]*ACMI\s+AP(?:\s+Seal)?[^.!?]*[.!?]?"),
        )
        for claim_type, pattern in claim_patterns:
            if match := re.search(pattern, description, re.I):
                claims.append({
                    "type": claim_type,
                    "claim": True,
                    "evidence": match.group(0).strip(),
                    "basis": "product_description",
                })

        supplier_reference = _clean(details.get("reference") or product.get("sku"))
        product.update({
            "name": details.get("name") or product.get("name"),
            "description": details.get("description") or product.get("description"),
            "brand": {"@type": "Brand", "name": brand} if brand else product.get("brand"),
            "sku": _manufacturer_sku(
                brand,
                _clean(details.get("name") or product.get("name")),
                str(details.get("link") or product.get("url") or page_url),
                supplier_reference,
            ),
            "supplier_reference": supplier_reference or None,
            "url": details.get("link") or product.get("url") or page_url,
            "image": images or product.get("image"),
            "category": details.get("category_name") or product.get("category"),
            "family": self._family(category),
            "material_kind": category,
            "firing_range": _firing_range(description),
            "technical_attributes": features,
            "documents": documents,
            "claims": claims,
            "all_image_urls": images,
            "source_updated_at": details.get("date_upd"),
            "stock_quantity": details.get("quantity"),
            "vat_status": "inclusive",
            "source_detail_level": "product_page",
        })
        return [product]


PROFILES = {
    "woocommerce": SupplierAdapter("woocommerce", "woocommerce", "fallback"),
    "prestashop": SupplierAdapter("prestashop", "prestashop", "fallback"),
    "shopify": SupplierAdapter("shopify", "shopify", "fallback"),
    "shopware": SupplierAdapter("shopware", "shopware", "fallback"),
    "starweb": SupplierAdapter("starweb", "starweb", "fallback"),
    "bigcommerce": SupplierAdapter("bigcommerce", "bigcommerce", "always"),
    "wix": SupplierAdapter("wix", "wix", "always"),
    "custom": SupplierAdapter("custom", "custom", "fallback"),
    "browser": SupplierAdapter("browser", "custom", "always"),
    "sio2": Sio2Adapter("sio2", "prestashop", "fallback"),
}


def adapter_for(source: dict[str, Any]) -> SupplierAdapter:
    return PROFILES[source.get("adapter", "custom")]
