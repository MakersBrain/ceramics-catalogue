"""Read schema.org microdata into the shape JSON-LD would have produced.

Several storefronts describe a product with `itemprop`/`itemtype` attributes
instead of a JSON-LD block. Rather than carry a second extraction path, this
rewrites a microdata scope into the dict `jsonld.products()` would have
returned, so every caller keeps reading one shape.

Parsing is regex-based like the rest of this package: the pages that need it are
server-rendered and the markup we care about is shallow.
"""

from __future__ import annotations

import html
import re
from typing import Any

from . import domain

#: An element that opens a schema.org scope, e.g. itemtype="https://schema.org/Product".
ITEMTYPE = re.compile(r'itemtype\s*=\s*["\']\s*https?://schema\.org/([A-Za-z]+)', re.I)
ITEMPROP = re.compile(r'itemprop\s*=\s*["\']([^"\']+)["\']', re.I)
OPEN_TAG = re.compile(r"<\s*([A-Za-z][A-Za-z0-9]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>", re.S)

#: Elements that never carry a closing tag, so a scope cannot be nested in them.
VOID = {"meta", "link", "img", "br", "hr", "input", "source", "area", "base", "col"}

#: Properties that may legitimately repeat and are worth keeping as a list.
MULTI = {"image", "additionalProperty"}


def products(document: str) -> list[dict[str, Any]]:
    """Return every microdata Product on the page, JSON-LD shaped."""
    found: list[dict[str, Any]] = []
    for match in ITEMTYPE.finditer(document):
        if match.group(1).casefold() != "product":
            continue
        start = document.rfind("<", 0, match.start())
        if start < 0:
            continue
        opening = OPEN_TAG.match(document, start)
        if opening is None:
            continue
        scope = _scope(document, start, opening.group(1))
        item = _read(scope[opening.end() - start:])
        if item:
            item.setdefault("@type", "Product")
            found.append(item)
    return found


def _scope(document: str, start: int, tag: str) -> str:
    """Return the markup of the element at `start`, including its children."""
    if tag.casefold() in VOID:
        end = document.find(">", start)
        return document[start: end + 1 if end != -1 else len(document)]
    depth = 0
    # the closing `>` must be consumed too, or the caller's tag-strip leaves a stub
    for match in re.finditer(rf"<\s*(/?)\s*{re.escape(tag)}\b[^>]*>", document[start:], re.I):
        if match.group(1):
            depth -= 1
            if depth <= 0:
                return document[start: start + match.end()]
        else:
            depth += 1
    return document[start:]


def _read(fragment: str) -> dict[str, Any]:
    """Read one scope's properties, descending into nested scopes."""
    item: dict[str, Any] = {}
    skip_to = 0
    for match in ITEMPROP.finditer(fragment):
        if match.start() < skip_to:
            continue  # belongs to a nested scope already consumed
        start = fragment.rfind("<", 0, match.start())
        if start < 0:
            continue
        opening = OPEN_TAG.match(fragment, start)
        if opening is None:
            continue
        tag, attributes = opening.group(1), opening.group(2)
        name = match.group(1).split()[0] if match.group(1).strip() else ""
        if not name:
            continue
        if "itemscope" in attributes.casefold() or ITEMTYPE.search(attributes):
            nested = _scope(fragment, start, tag)
            skip_to = start + len(nested)
            inner = nested[opening.end() - start:]
            value: Any = _read(inner)
            if not value:
                value = _text(inner)
        else:
            value = _value(tag, attributes, fragment, start, opening.end())
        if value in (None, "", {}):
            continue
        _assign(item, name, value)
    return item


def _value(tag: str, attributes: str, fragment: str, start: int, after_open: int) -> str | None:
    """Take a property's value from the attribute that carries it, else the text."""
    for attribute in ("content", "datetime"):
        if found := re.search(rf'{attribute}\s*=\s*["\']([^"\']*)', attributes, re.I):
            return html.unescape(found.group(1)).strip()
    lowered = tag.casefold()
    if lowered in ("img", "source", "audio", "video", "embed"):
        source = re.search(r'(?:src|content)\s*=\s*["\']([^"\']*)', attributes, re.I)
        return html.unescape(source.group(1)).strip() if source else None
    if lowered in ("a", "link", "area"):
        href = re.search(r'href\s*=\s*["\']([^"\']*)', attributes, re.I)
        if href:
            return html.unescape(href.group(1)).strip()
    if lowered in VOID:
        return None
    return _text(_scope(fragment, start, tag)[after_open - start:])


def _text(fragment: str) -> str | None:
    return domain.clean(re.sub(r"<[^>]+>", " ", fragment))


def _assign(item: dict[str, Any], name: str, value: Any) -> None:
    """Keep repeated properties as a list, and never let a later value win."""
    if name not in item:
        item[name] = [value] if name in MULTI else value
        return
    existing = item[name]
    if isinstance(existing, list):
        if value not in existing:
            existing.append(value)
    elif name in MULTI and existing != value:
        item[name] = [existing, value]
