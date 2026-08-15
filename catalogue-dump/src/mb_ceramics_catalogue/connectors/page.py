"""Reusable, dependency-free HTML discovery and structured-data helpers."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from pydantic import BaseModel, ConfigDict, Field, model_validator


def clean(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", str(value))).split())


def compatibility_clean(value: Any) -> str:
    """Legacy-compatible text cleanup for adapters requiring byte parity."""

    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).split())


def balanced_object(document: str, start: int) -> dict[str, Any] | None:
    """Read a complete JSON object at ``start``, respecting quoted braces."""

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
                    value = json.loads(document[start : index + 1])
                except json.JSONDecodeError:
                    return None
                return value if isinstance(value, dict) else None
    return None


def canonical(url: str) -> str:
    parsed = urlparse(url)
    query = (
        ""
        if re.search(
            r"(?:^|&)(?:order|tag|id_currency|search_query|back|q|sort)=", parsed.query
        )
        else parsed.query
    )
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, query, ""))


def probable_javascript_shell(document: str) -> bool:
    lower = document.casefold()
    if "<html" not in lower and "<!doctype" not in lower:
        return False
    explicit = any(
        marker in lower
        for marker in (
            "enable javascript",
            "javascript is required",
            "requires javascript",
            'id="__next"',
            "id='__next'",
            'id="root"',
            "id='root'",
            'id="app"',
            "id='app'",
            "ng-version=",
        )
    )
    scripts = len(re.findall(r"<script\b", lower))
    visible = re.sub(
        r"<script\b[\s\S]*?</script>|<style\b[\s\S]*?</style>|<[^>]+>", " ", lower
    )
    return explicit and scripts > 0 and len(" ".join(html.unescape(visible).split())) < 1000


def links(document: str, page_url: str, *, cards_only: bool = False) -> list[str]:
    origin = urlparse(page_url).netloc
    scope = document
    if cards_only:
        cards = re.findall(
            r'<(?:article|li|div)[^>]*class=["\'][^"\']*(?:product-miniature|product-item|product-card|productbox)[^"\']*["\'][\s\S]*?</(?:article|li|div)>',
            document,
            re.IGNORECASE,
        )
        pagination = re.findall(
            r'<a[^>]+(?:rel=["\']next["\']|class=["\'][^"\']*(?:next|pagination)[^"\']*["\'])[^>]*>',
            document,
            re.IGNORECASE,
        )
        scope = "".join((*cards, *pagination)) or document
    found = []
    for match in re.finditer(r'href=["\']([^"\']+)["\']', scope, re.IGNORECASE):
        candidate = canonical(urljoin(page_url, html.unescape(match.group(1))))
        if urlparse(candidate).netloc == origin:
            found.append(candidate)
    return list(dict.fromkeys(found))


def sitemap_locations(document: str) -> tuple[bool, list[str]]:
    locations = [
        clean(value)
        for value in re.findall(
            r"<loc>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</loc>",
            document,
            re.IGNORECASE,
        )
    ]
    return bool(re.search(r"<sitemapindex\b", document, re.IGNORECASE)), [
        value for value in locations if value
    ]


def jsonld_blocks(document: str) -> list[dict[str, Any]]:
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
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        document,
        re.IGNORECASE | re.DOTALL,
    ):
        try:
            flatten(json.loads(html.unescape(raw.strip())))
        except (json.JSONDecodeError, TypeError):
            continue
    return found


def jsonld_products(document: str) -> list[dict[str, Any]]:
    return [item for item in jsonld_blocks(document) if _has_type(item, "product")]


def breadcrumbs(document: str) -> list[str]:
    for item in jsonld_blocks(document):
        if not _has_type(item, "breadcrumblist"):
            continue
        names: list[str] = []
        for element in item.get("itemListElement") or []:
            if not isinstance(element, dict):
                continue
            entry = element.get("item")
            name = entry.get("name") if isinstance(entry, dict) else element.get("name")
            if cleaned := clean(name):
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
        if match := re.search(pattern, document, re.IGNORECASE):
            return html.unescape(match.group(1)).strip()
    return None


def offer(item: dict[str, Any]) -> dict[str, Any]:
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
        if cleaned := clean(entry):
            result.append(urljoin(page_url, cleaned))
    return list(dict.fromkeys(result))


def brand(item: dict[str, Any]) -> str | None:
    value = item.get("brand")
    if isinstance(value, dict):
        value = value.get("name")
    if isinstance(value, list) and value:
        value = value[0].get("name") if isinstance(value[0], dict) else value[0]
    return clean(value) or None


def gtin(item: dict[str, Any]) -> str | None:
    for key in ("gtin13", "gtin14", "gtin12", "gtin8", "gtin", "ean"):
        if value := clean(item.get(key)):
            return value
    return None


def pdf_links(document: str, page_url: str) -> list[tuple[str, str]]:
    return [
        (urljoin(page_url, html.unescape(match.group(1))), clean(match.group(2)))
        for match in re.finditer(
            r'<a[^>]+href=["\']([^"\']+\.pdf[^"\']*)["\'][^>]*>(.*?)</a>',
            document,
            re.IGNORECASE | re.DOTALL,
        )
    ]


DOCUMENT_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "safety_data_sheet",
        r"\b(?:msds|sds|safety data|fiche de s[eé]curit[eé]|s[eé]curit[eé]|sicherheitsdatenblatt|veiligheidsblad|scheda di sicurezza)\b",
    ),
    (
        "technical_sheet",
        r"\b(?:tds|technical data|technical sheet|fiche technique|datenblatt|technisch|scheda tecnica|ficha t[eé]cnica)\b",
    ),
    ("certificate", r"\b(?:certificat|certificate|zertifikat|certificaat|certificato)\b"),
    (
        "lab_report",
        r"\b(?:lab(?:oratory)? (?:report|result)|rapport d'analyse|pr[uü]fbericht)\b",
    ),
    ("instructions", r"\b(?:instruction|mode d'emploi|anleitung|handleiding|istruzioni)\b"),
)


def documents(links: Iterable[tuple[str, str]], page_url: str = "") -> list[dict[str, Any]]:
    """Classify linked technical documents without dataset-layer dependencies."""

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_url, raw_label in links:
        url = urljoin(page_url, compatibility_clean(raw_url)) if page_url else compatibility_clean(raw_url)
        label = compatibility_clean(raw_label)
        if not url or url in seen:
            continue
        haystack = f"{label} {urlparse(url).path}"
        document_type = next(
            (name for name, pattern in DOCUMENT_PATTERNS if re.search(pattern, haystack, re.I)),
            None,
        )
        if document_type is None:
            continue
        seen.add(url)
        result.append({"type": document_type, "name": label or None, "url": url})
    return result


NON_SPECIFICATION = re.compile(
    r"cookie|consent|privacy|gdpr|rgpd|datenschutz|expiry|provider purpose"
    r"|prestashop-#|_ga\b|session|newsletter|shipping cost|delivery time"
    r"|analytic|tracking|tracker|visitor|advertis|google|facebook|hotjar",
    re.IGNORECASE,
)


def specification_table(document: str) -> dict[str, str]:
    attributes: dict[str, str] = {}
    patterns = (
        r"<tr[^>]*>\s*<t[hd][^>]*>(.*?)</t[hd]>\s*<t[hd][^>]*>(.*?)</t[hd]>\s*</tr>",
        r'<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, document, re.IGNORECASE | re.DOTALL):
            name, value = clean(match.group(1)).rstrip(":"), clean(match.group(2))
            if (
                name
                and value
                and len(name) < 60
                and not NON_SPECIFICATION.search(name)
                and not NON_SPECIFICATION.search(value)
            ):
                attributes.setdefault(name, value)
    return attributes


class DomFieldSelector(BaseModel):
    """Small verified CSS subset; arbitrary executable browser selectors are excluded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    selector: str = Field(min_length=1)
    attribute: str | None = None

    @model_validator(mode="after")
    def _supported_selector(self) -> DomFieldSelector:
        if not re.fullmatch(
            r"(?:[A-Za-z][\w-]*)?(?:#[\w-]+|\.[\w-]+|\[[\w:-]+(?:=[\"\']?[\w:./ -]+[\"\']?)?\])?",
            self.selector,
        ):
            raise ValueError("DOM rules support one tag/id/class/attribute selector only")
        if self.attribute is not None and not re.fullmatch(r"[\w:-]+", self.attribute):
            raise ValueError("DOM rule attribute is invalid")
        return self


class VerifiedDomRules(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verification: tuple[DomFieldSelector, ...] = Field(min_length=1)
    name: DomFieldSelector
    price: DomFieldSelector | None = None
    currency: DomFieldSelector | None = None
    description: DomFieldSelector | None = None
    sku: DomFieldSelector | None = None
    image: DomFieldSelector | None = None
    availability: DomFieldSelector | None = None


def select(document: str, rule: DomFieldSelector) -> str | None:
    parsed = _selector(rule.selector)
    if parsed is None:
        return None
    tag, wanted_id, wanted_class, wanted_attr, wanted_value = parsed
    tag_pattern = tag or r"[A-Za-z][\w-]*"
    for match in re.finditer(
        rf"<(?P<tag>{tag_pattern})\b(?P<attrs>(?:[^>\"']|\"[^\"]*\"|'[^']*')*)>",
        document,
        re.IGNORECASE | re.DOTALL,
    ):
        attrs = _attributes(match.group("attrs"))
        if wanted_id and attrs.get("id") != wanted_id:
            continue
        if wanted_class and wanted_class not in (attrs.get("class") or "").split():
            continue
        if wanted_attr and wanted_attr not in attrs:
            continue
        if wanted_value is not None and attrs.get(wanted_attr or "") != wanted_value:
            continue
        if rule.attribute:
            return clean(attrs.get(rule.attribute)) or None
        if match.group("tag").casefold() in {"meta", "link", "img", "input", "source"}:
            return clean(attrs.get("content") or attrs.get("src") or attrs.get("value")) or None
        close = re.search(
            rf"</{re.escape(match.group('tag'))}\s*>",
            document[match.end() :],
            re.IGNORECASE,
        )
        end = match.end() + close.start() if close else len(document)
        return clean(document[match.end() : end]) or None
    return None


def microdata_products(document: str) -> list[dict[str, Any]]:
    """Read Product microdata into the JSON-LD shape used by the parser."""
    found: list[dict[str, Any]] = []
    itemtype = re.compile(
        r'itemtype\s*=\s*["\']\s*https?://schema\.org/([A-Za-z]+)', re.IGNORECASE
    )
    opening_tag = re.compile(
        r"<\s*([A-Za-z][A-Za-z0-9]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>",
        re.DOTALL,
    )
    for match in itemtype.finditer(document):
        if match.group(1).casefold() != "product":
            continue
        start = document.rfind("<", 0, match.start())
        opening = opening_tag.match(document, start) if start >= 0 else None
        if opening is None:
            continue
        scope = _microdata_scope(document, start, opening.group(1))
        item = _microdata_read(scope[opening.end() - start :])
        if item:
            item.setdefault("@type", "Product")
            found.append(item)
    return found


_VOID_TAGS = {"meta", "link", "img", "br", "hr", "input", "source", "area", "base", "col"}


def _microdata_scope(document: str, start: int, tag: str) -> str:
    if tag.casefold() in _VOID_TAGS:
        end = document.find(">", start)
        return document[start : end + 1 if end != -1 else len(document)]
    depth = 0
    for match in re.finditer(
        rf"<\s*(/?)\s*{re.escape(tag)}\b[^>]*>", document[start:], re.IGNORECASE
    ):
        if match.group(1):
            depth -= 1
            if depth <= 0:
                return document[start : start + match.end()]
        else:
            depth += 1
    return document[start:]


def _microdata_read(fragment: str) -> dict[str, Any]:
    item: dict[str, Any] = {}
    skip_to = 0
    itemprop = re.compile(r'itemprop\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
    opening_tag = re.compile(
        r"<\s*([A-Za-z][A-Za-z0-9]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>",
        re.DOTALL,
    )
    for match in itemprop.finditer(fragment):
        if match.start() < skip_to:
            continue
        start = fragment.rfind("<", 0, match.start())
        opening = opening_tag.match(fragment, start) if start >= 0 else None
        if opening is None:
            continue
        tag, attributes = opening.group(1), opening.group(2)
        name = match.group(1).split()[0] if match.group(1).strip() else ""
        if not name:
            continue
        if "itemscope" in attributes.casefold() or re.search(
            r"itemtype\s*=", attributes, re.IGNORECASE
        ):
            nested = _microdata_scope(fragment, start, tag)
            skip_to = start + len(nested)
            value: Any = _microdata_read(nested[opening.end() - start :])
            if not value:
                value = clean(nested[opening.end() - start :])
        else:
            value = _microdata_value(tag, attributes, fragment, start, opening.end())
        if value not in (None, "", {}):
            _microdata_assign(item, name, value)
    return item


def _microdata_value(
    tag: str, attributes: str, fragment: str, start: int, after_open: int
) -> str | None:
    attrs = _attributes(attributes)
    for attribute in ("content", "datetime"):
        if value := attrs.get(attribute):
            return value
    lowered = tag.casefold()
    if lowered in {"img", "source", "audio", "video", "embed"}:
        return attrs.get("src") or attrs.get("content")
    if lowered in {"a", "link", "area"} and attrs.get("href"):
        return attrs["href"]
    if lowered in _VOID_TAGS:
        return None
    return clean(_microdata_scope(fragment, start, tag)[after_open - start :]) or None


def _microdata_assign(item: dict[str, Any], name: str, value: Any) -> None:
    if name not in item:
        item[name] = [value] if name in {"image", "additionalProperty"} else value
    elif isinstance(item[name], list):
        if value not in item[name]:
            item[name].append(value)
    elif name in {"image", "additionalProperty"} and item[name] != value:
        item[name] = [item[name], value]


def _has_type(item: dict[str, Any], wanted: str) -> bool:
    types = item.get("@type", [])
    types = types if isinstance(types, list) else [types]
    return any(str(value).casefold() == wanted for value in types)


def _attributes(raw: str) -> dict[str, str]:
    return {
        match.group(1).casefold(): html.unescape(match.group(3) or match.group(4) or match.group(5) or "")
        for match in re.finditer(
            r"([\w:-]+)\s*=\s*(\"([^\"]*)\"|'([^']*)'|([^\s>]+))", raw
        )
    }


def _selector(
    value: str,
) -> tuple[str | None, str | None, str | None, str | None, str | None] | None:
    match = re.fullmatch(
        r"(?P<tag>[A-Za-z][\w-]*)?(?:#(?P<id>[\w-]+)|\.(?P<class>[\w-]+)|\[(?P<attr>[\w:-]+)(?:=[\"\']?(?P<value>[\w:./ -]+)[\"\']?)?\])?",
        value,
    )
    if not match:
        return None
    return (
        match.group("tag"),
        match.group("id"),
        match.group("class"),
        match.group("attr"),
        match.group("value"),
    )
