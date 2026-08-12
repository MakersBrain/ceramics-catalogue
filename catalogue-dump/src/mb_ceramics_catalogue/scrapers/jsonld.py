"""Shared extraction helpers for server-rendered product pages."""

from __future__ import annotations

import html
import json
import re
from typing import Any
from urllib.parse import urljoin

from . import domain


def blocks(document: str) -> list[dict[str, Any]]:
    """Return every JSON-LD object in a page, flattening @graph containers."""
    found: list[dict[str, Any]] = []

    def flatten(value: Any) -> None:
        if isinstance(value, list):
            for child in value:
                flatten(child)
        elif isinstance(value, dict):
            if "@graph" in value:
                flatten(value["@graph"])
            else:
                found.append(value)

    for raw in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', document, re.I | re.S,
    ):
        try:
            flatten(json.loads(html.unescape(raw.strip())))
        except (json.JSONDecodeError, TypeError):
            continue
    return found


def products(document: str) -> list[dict[str, Any]]:
    """Select the JSON-LD objects that are actually Products."""
    result = []
    for item in blocks(document):
        types = item.get("@type", [])
        types = types if isinstance(types, list) else [types]
        if any(str(value).casefold() == "product" for value in types):
            result.append(item)
    return result


def breadcrumbs(document: str) -> list[str]:
    for item in blocks(document):
        types = item.get("@type", [])
        types = types if isinstance(types, list) else [types]
        if not any(str(value).casefold() == "breadcrumblist" for value in types):
            continue
        names = []
        for element in item.get("itemListElement") or []:
            if not isinstance(element, dict):
                continue
            entry = element.get("item")
            name = entry.get("name") if isinstance(entry, dict) else element.get("name")
            if cleaned := domain.clean(name):
                names.append(cleaned)
        if names:
            return names
    return []


def meta(document: str, key: str) -> str | None:
    escaped = re.escape(key)
    for pattern in (
        rf'<meta[^>]+(?:property|name)=["\']{escaped}["\'][^>]+content=["\']([^"\']*)',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']{escaped}["\']',
    ):
        if match := re.search(pattern, document, re.I):
            return html.unescape(match.group(1)).strip()
    return None


def offer(item: dict[str, Any]) -> dict[str, Any]:
    """Take the first usable offer from a Product's offers field."""
    offers = item.get("offers")
    if isinstance(offers, list):
        for candidate in offers:
            if isinstance(candidate, dict) and candidate.get("price") is not None:
                return candidate
        offers = offers[0] if offers else {}
    if isinstance(offers, dict):
        if offers.get("@type") == "AggregateOffer" and offers.get("lowPrice") is not None:
            return {**offers, "price": offers.get("lowPrice")}
        return offers
    return {}


def images(item: dict[str, Any], page_url: str) -> list[str]:
    value = item.get("image")
    values = value if isinstance(value, list) else [value]
    result = []
    for entry in values:
        if isinstance(entry, dict):
            entry = entry.get("url") or entry.get("contentUrl")
        if cleaned := domain.clean(entry):
            result.append(urljoin(page_url, cleaned))
    return list(dict.fromkeys(result))


def brand(item: dict[str, Any]) -> str | None:
    value = item.get("brand")
    if isinstance(value, dict):
        value = value.get("name")
    if isinstance(value, list) and value:
        value = value[0].get("name") if isinstance(value[0], dict) else value[0]
    return domain.clean(value) or None


def gtin(item: dict[str, Any]) -> str | None:
    for key in ("gtin13", "gtin14", "gtin12", "gtin8", "gtin", "ean"):
        if value := domain.clean(item.get(key)):
            return value
    return None


def pdf_links(document: str, page_url: str) -> list[tuple[str, str]]:
    """Collect (url, label) pairs for linked PDF documents."""
    pairs = []
    for match in re.finditer(
        r'<a[^>]+href=["\']([^"\']+\.pdf[^"\']*)["\'][^>]*>(.*?)</a>', document, re.I | re.S,
    ):
        pairs.append((urljoin(page_url, html.unescape(match.group(1))), domain.clean(match.group(2))))
    return pairs


#: Two-column tables that are not product specifications. Cookie policies are
#: the common offender: they sit in the same markup as a spec table and would
#: otherwise land in technical_attributes.
NON_SPECIFICATION = re.compile(
    r"cookie|consent|privacy|gdpr|rgpd|datenschutz|expiry|provider purpose"
    r"|prestashop-#|_ga\b|session|newsletter|shipping cost|delivery time"
    r"|analytic|tracking|tracker|visitor|advertis|google|facebook|hotjar",
    re.I,
)


def specification_table(document: str) -> dict[str, str]:
    """Read a two-column specification table into name/value pairs."""
    attributes: dict[str, str] = {}
    for match in re.finditer(
        r"<tr[^>]*>\s*<t[hd][^>]*>(.*?)</t[hd]>\s*<t[hd][^>]*>(.*?)</t[hd]>\s*</tr>", document, re.I | re.S,
    ):
        _keep(attributes, match.group(1), match.group(2))
    for match in re.finditer(
        r'<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>', document, re.I | re.S,
    ):
        _keep(attributes, match.group(1), match.group(2))
    return attributes


def _keep(attributes: dict[str, str], raw_name: str, raw_value: str) -> None:
    name, value = domain.clean(raw_name).rstrip(":"), domain.clean(raw_value)
    if not name or not value or len(name) >= 60:
        return
    if NON_SPECIFICATION.search(name) or NON_SPECIFICATION.search(value):
        return
    attributes.setdefault(name, value)


def availability(value: Any) -> str | None:
    text = domain.clean(value)
    if not text:
        return None
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    mapping = {
        "instock": "https://schema.org/InStock",
        "outofstock": "https://schema.org/OutOfStock",
        "preorder": "https://schema.org/PreOrder",
        "backorder": "https://schema.org/BackOrder",
        "limitedavailability": "https://schema.org/LimitedAvailability",
        "discontinued": "https://schema.org/Discontinued",
    }
    return mapping.get(text.casefold().replace(" ", ""), f"https://schema.org/{text}")
