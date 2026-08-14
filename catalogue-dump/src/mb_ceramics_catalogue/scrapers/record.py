"""The ceramics.catalogue_item.v2 record: one row per purchasable variant.

A supplier product sold in three jar sizes becomes three rows sharing a
parent_external_id. That is the only shape in which a price per litre can be
compared across suppliers, which is the point of the dump.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from . import domain

RECORD_FORMAT = "ceramics.catalogue_item.v2"
#: Manufacturer catalogues publish identities and specifications but no price.
IDENTITY_FORMAT = "ceramics.catalogue_identity.v2"

CURRENCY_SYMBOLS = {
    "€": "EUR", "$": "USD", "£": "GBP", "CHF": "CHF", "kr": "SEK",
    "zł": "PLN", "Kč": "CZK", "Ft": "HUF", "лв": "BGN",
}

VAT_INCLUSIVE = re.compile(r"\bTTC\b|incl(?:usive|uding|\.)?\s*(?:VAT|BTW|MwSt|IVA|tax)|inkl\.?\s*MwSt|prijs incl", re.I)
VAT_EXCLUSIVE = re.compile(r"\bHT\b|\bexcl(?:usive|uding|\.)?\s*(?:VAT|BTW|MwSt|IVA|tax)|zzgl\.?\s*MwSt|hors taxe", re.I)


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_price(value: Any) -> tuple[float | None, str | None]:
    """Read an amount and currency from a price string or a bare number."""
    if isinstance(value, (int, float)):
        return float(value), None
    text = domain.clean(value)
    if not text:
        return None, None
    code = re.search(r"\b(EUR|USD|GBP|CHF|SEK|NOK|DKK|PLN|CZK|HUF|BGN|RON)\b", text, re.I)
    currency = code.group(1).upper() if code else None
    if currency is None:
        for symbol, mapped in CURRENCY_SYMBOLS.items():
            if symbol in text:
                currency = mapped
                break
    number = re.sub(r"[^0-9.,-]", "", text)
    number = re.sub(r"[.,](?=\d{3}(?:\D|$))", "", number)
    number = number.replace(",", ".")
    try:
        return float(number), currency
    except ValueError:
        return None, currency


def vat_status(*texts: Any, default: str | None = None) -> str | None:
    haystack = " ".join(domain.clean(text) for text in texts if text)
    if VAT_EXCLUSIVE.search(haystack):
        return "exclusive"
    if VAT_INCLUSIVE.search(haystack):
        return "inclusive"
    return default


#: What each source is, for the task currently building records.
#:
#: A manufacturer's own shop is the one place a bare article number *is* the
#: manufacturer's code — "1050 UNDERGLAZE BASE" on spectrumglazes.com is
#: Spectrum 1050. On a retailer's shelf the same number is that retailer's
#: reference and means nothing to anyone else, so it is never promoted there.
#:
#: This was a module-level dict mutated by `learn_sources()` at the start of a
#: run. That is fine in a process that crawls once and exits and wrong in a
#: worker handling thousands of jobs, where two jobs can hold different views of
#: a source at once — an operator override in `catalogue.source_settings` is
#: exactly that. A ContextVar is the same shape as `activity.CURRENT_SOURCE`
#: and gives each job's task its own binding, which is why no scraper had to
#: change: they all reach the traits through `build()`, never directly.
#:
#: The default is a genuinely immutable mapping rather than a bare `{}`: a
#: mutable ContextVar default is shared by every context that never set one, so
#: a single accidental write would leak source traits across unrelated jobs —
#: precisely the bug this whole change is removing.
SOURCE_TRAITS: ContextVar[Mapping[str, Mapping[str, Any]]] = ContextVar(
    "catalogue_source_traits", default=MappingProxyType({})
)


def traits_for(sources: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Reduce a sources file to the two facts `build()` needs from it."""
    return {
        name: {
            "brand": source.get("brand"),
            "is_manufacturer": bool(source.get("is_manufacturer")),
        }
        for name, source in sources.items()
    }


class RecordBuilder:
    """The source traits one crawl builds records against.

    Constructed from a sources file and entered around the work that uses it:

        with RecordBuilder(sources):
            await scraper.run(limit)

    A worker enters one per job, inside that job's task, so concurrent jobs
    never see each other's configuration.
    """

    __slots__ = ("_token", "traits")

    def __init__(self, sources: Mapping[str, Mapping[str, Any]]) -> None:
        self.traits = traits_for(sources)
        self._token: Any = None

    def bind(self) -> None:
        """Install these traits for the current task and everything it starts."""
        self._token = SOURCE_TRAITS.set(self.traits)

    def unbind(self) -> None:
        if self._token is not None:
            SOURCE_TRAITS.reset(self._token)
            self._token = None

    def __enter__(self) -> RecordBuilder:
        self.bind()
        return self

    def __exit__(self, *_: object) -> None:
        self.unbind()


def learn_sources(config: Mapping[str, Mapping[str, Any]]) -> None:
    """Bind source traits for the current context, permanently.

    Kept for the single-run entry points, where "for the rest of this process"
    is the correct scope and a context manager would only add a level of
    indentation around the whole of `main()`.
    """
    SOURCE_TRAITS.set(traits_for(config))


def build(
    *,
    source: str,
    product_url: str,
    name: str,
    price: float | None,
    currency: str | None,
    extraction_method: str,
    variant_id: str | None = None,
    variant_title: str | None = None,
    product_name: str | None = None,
    parent_url: str | None = None,
    brand: str | None = None,
    manufacturer_sku: str | None = None,
    supplier_reference: str | None = None,
    gtin: str | None = None,
    description: str | None = None,
    category_path: list[str] | None = None,
    image_url: str | None = None,
    all_image_urls: list[str] | None = None,
    price_text: str | None = None,
    list_price: float | None = None,
    vat: str | None = None,
    vat_rate: float | None = None,
    availability: str | None = None,
    stock_quantity: int | None = None,
    min_order_quantity: int | None = None,
    documents: list[dict[str, Any]] | None = None,
    technical_attributes: dict[str, Any] | None = None,
    source_detail_level: str = "api",
    source_updated_at: str | None = None,
    identity_only: bool = False,
    claims: list[dict[str, Any]] | None = None,
    source_brand: str | None = None,
    source_is_manufacturer: bool = False,
    raw: Any = None,
) -> dict[str, Any]:
    """Assemble one variant row, deriving every ceramics field from its text."""
    category_path = category_path or []
    product_name = product_name or name
    specification = " ".join(
        f"{key}: {value}"
        for key, value in (technical_attributes or {}).items()
        if isinstance(value, (str, int, float))
    )
    derived = domain.describe(
        name, description, category_path, specification,
        colour_hint=domain.attribute_colour(technical_attributes),
    )
    # A variant title normally carries the package size ("473 ml", "1 pint"); a
    # specification table is the next best source, then the product name itself.
    liquid = derived["form"] == "liquid" or derived["family"] in {"glaze", "underglaze", "engobe"}
    package = (
        domain.package_size(variant_title or "", liquid_hint=liquid)
        or domain.package_size_from_attributes(technical_attributes, liquid_hint=liquid)
        or domain.package_size(name, description or "", liquid_hint=liquid)
        or derived["package_size"]
    )

    # The published title, and what can be read out of it. Both are kept: the
    # parsed name is what a reader searches and compares, and the raw one is
    # what the supplier actually wrote, which is the only defensible thing to
    # show beside a price.
    traits = SOURCE_TRAITS.get().get(source, {})
    title = domain.parse_title(
        name,
        package=package,
        supplier_sku=supplier_reference,
        source_brand=source_brand or traits.get("brand"),
        source_is_manufacturer=source_is_manufacturer or bool(traits.get("is_manufacturer")),
        published_brand=brand,
    )
    brand = title["brand"] or brand
    manufacturer_sku = manufacturer_sku or title["code"]

    # The parent groups variants, so it must not carry a variant-selecting query.
    parent = parent_url or re.sub(r"\?.*$", "", product_url)
    external_id = f"{source}:{parent}"
    if variant_id:
        external_id = f"{external_id}#{variant_id}"
    if identity_only:
        price = list_price = None

    # An explicit OutOfStock offer has exactly zero immediately sellable units,
    # regardless of whether the platform publishes a separate counter. Some
    # PrestaShop installations expose negative internal inventory after an
    # oversell; that is not a negative number of units a buyer can purchase.
    if availability and availability.rstrip("/").rsplit("/", 1)[-1].casefold() == "outofstock":
        stock_quantity = 0
    elif (
        not isinstance(stock_quantity, int)
        or isinstance(stock_quantity, bool)
        or stock_quantity < 0
    ):
        stock_quantity = None

    return {
        "format": IDENTITY_FORMAT if identity_only else RECORD_FORMAT,
        "source": source,
        "external_id": external_id,
        "parent_external_id": f"{source}:{parent}",
        "product_url": product_url,
        "extraction_method": extraction_method,
        "source_detail_level": source_detail_level,
        "fetched_at": now(),
        "source_updated_at": source_updated_at,

        # `name` is the parsed one, because it is what everything downstream
        # searches, groups and prints; `name_raw` is the title as published and
        # is never edited, so a row can always be checked against the shop.
        "name": title["name"],
        "name_raw": title["name_raw"],
        "name_parsed_from": title["evidence"] or None,
        "product_name": domain.clean(product_name) or None,
        "variant_title": domain.clean(variant_title) or None,
        "brand": domain.clean(brand) or None,
        "brand_basis": title["brand_basis"],
        "manufacturer_sku_basis": title["code_basis"],
        "manufacturer_sku": domain.clean(manufacturer_sku) or None,
        "supplier_reference": domain.clean(supplier_reference) or None,
        "gtin": domain.clean(gtin) or None,
        "description": domain.clean(description) or None,
        "category_path": category_path or None,
        "image_url": image_url,
        "all_image_urls": all_image_urls or ([image_url] if image_url else None),

        "price": price,
        "currency": currency,
        "price_text": price_text,
        "list_price": list_price,
        "vat_status": vat,
        "vat_rate": vat_rate,
        "unit_price": domain.unit_price(price, currency, package),
        "availability": availability,
        "stock_quantity": stock_quantity,
        "min_order_quantity": min_order_quantity,
        "package_size": package,

        "family": derived["family"],
        "form": derived["form"],
        "firing": derived["firing"],
        "surface": derived["surface"],
        "effects": derived["effects"] or None,
        "colour": derived["colour"],
        "application_methods": derived["application_methods"] or None,
        "coats": derived["coats"],
        # Text claims, then the ones a scraper read from a structured field.
        "claims": _merge_claims(
            derived["claims"], domain.attribute_claims(technical_attributes), claims or [],
        ) or None,
        "documents": documents or None,
        "technical_attributes": technical_attributes or None,

        "raw": raw,
    }


def _merge_claims(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one claim per type, preferring a structured field over prose.

    A specification field is a more reliable statement than a sentence matched
    in marketing copy, so a later group overrides an earlier one.
    """
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for claim in group:
            if isinstance(claim, dict) and claim.get("type"):
                merged[claim["type"]] = claim
    return list(merged.values())


def is_valid(record: dict[str, Any]) -> bool:
    """Require an identity, and a usable price unless this is an identity row."""
    if not record.get("name"):
        return False
    if record.get("format") == IDENTITY_FORMAT:
        return True
    price = record.get("price")
    return isinstance(price, (int, float)) and price >= 0


def in_scope(record: dict[str, Any], strict: bool = True) -> bool:
    """Apply the ceramic-materials scope decided for this catalogue.

    The keyword tests still read the identity only: glaze copy mentions brushes,
    kilns and shelves constantly, and running those words over free prose rejects
    the very products we want. The description is passed all the same, because
    the one thing it can be read for is a published electrical rating — a machine
    announces itself with a kilowatt in any language, and no glaze has one.
    """
    if not strict:
        return True
    categories = record.get("category_path") or []
    return domain.is_material(
        record.get("family"),
        record.get("name"),
        " ".join(categories),
        categories=categories,
        description=record.get("description") or "",
    )


def dedupe_key(record: dict[str, Any]) -> tuple[Any, ...]:
    """Collapse redirected or duplicated URLs without merging real variants."""
    identity = (
        domain.clean(record.get("manufacturer_sku")).casefold()
        or domain.clean(record.get("supplier_reference")).casefold()
        or domain.clean(record.get("name")).casefold()
    )
    package = record.get("package_size") or {}
    return (
        identity,
        record.get("price"),
        record.get("currency"),
        package.get("value"),
        package.get("unit"),
        domain.clean(record.get("variant_title")).casefold(),
    )


def price_fingerprint(record: dict[str, Any]) -> str:
    package = record.get("package_size") or {}
    context = {
        "price": record.get("price"),
        "currency": record.get("currency"),
        "vat_status": record.get("vat_status"),
        "quantity": package.get("value"),
        "unit": package.get("unit"),
    }
    return hashlib.sha256(json.dumps(context, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def coverage(records: list[dict[str, Any]]) -> dict[str, int]:
    """Count how many rows carry each field, so a thin scraper is visible."""
    tracked = (
        "manufacturer_sku", "supplier_reference", "gtin", "brand", "description",
        "package_size", "unit_price", "family", "form", "firing", "surface",
        "effects", "colour", "claims", "documents", "vat_status", "stock_quantity",
        "category_path", "all_image_urls",
    )
    return {field: sum(1 for record in records if record.get(field)) for field in tracked}
