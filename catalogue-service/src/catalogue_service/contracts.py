"""The catalogue read API, as models. The document is generated from these.

Writing the spec is what surfaced the shape corrections in §10.3, and each one
is a thing the old API did implicitly:

* **`/v1/canonical-products` was two operations wearing one path.** `?ids=`
  was a batch fetch returning offers; `?q=` was a search returning aggregates.
  One operation id, two response shapes, which OpenAPI can only express as a
  union every client then has to discriminate. Split into three operations.
* **Errors were `{"error": "..."}`** — an undocumented string. Now RFC 9457.
* **Search capped `limit` at 200 and returned no cursor**, so there was no way
  to read past the cap. The ordering was already deterministic, so a cursor was
  always available; it simply was not offered.
* **`min_price_per_litre` was a number with an implied currency** that was never
  stated, and is in fact a mix of currencies. It is now explicitly EUR at a
  stated reference rate, with the rate's date in the response.
* **`observed_at` was on the fetch path and not the search path**, so a consumer
  could not tell a price collected this morning from one collected in March.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

Family = Literal[
    "glaze", "underglaze", "engobe", "clay_body", "oxide", "stain", "raw_material", "other"
]


class FiringRange(BaseModel):
    """What a product's own text says about firing, never inferred."""

    min_celsius: int | None = None
    max_celsius: int | None = None
    cone_min: str | None = None
    cone_max: str | None = None
    cone_system: str | None = None
    published_unit: str | None = None


class PriceSummary(BaseModel):
    """A price range across the shops that carry a product.

    Always in `currency` at `rate_date`. The old API returned a bare number
    mixing EUR, USD and GBP, which made a "cheapest per litre" comparison
    quietly wrong for any product carried in more than one country.
    """

    currency: Literal["EUR"] = "EUR"
    rate_date: date | None = Field(
        default=None,
        description="The date of the reference rates used to convert other currencies.",
    )
    min_per_litre: float | None = None
    max_per_litre: float | None = None
    min_per_kilogram: float | None = None
    max_per_kilogram: float | None = None


class CanonicalProduct(BaseModel):
    """One product as a search result: enough to decide whether it is the thing."""

    canonical_product_id: str
    brand: str | None = None
    manufacturer_sku: str | None = None
    canonical_name: str
    family: Family | None = None
    firing_range: FiringRange | None = None
    source_count: int = Field(description="How many shops carry it.")
    price: PriceSummary


class Offer(BaseModel):
    """One shop's listing of one product."""

    source_id: str
    supplier_name: str | None = None
    supplier_reference: str | None = None
    product_url: str
    price: float | None = None
    currency: str | None = None
    vat_status: Literal["inclusive", "exclusive", "unknown"] | None = None
    package_quantity: float | None = None
    package_unit: str | None = None
    unit_price: float | None = None
    unit_price_per: Literal["l", "kg"] | None = None
    availability: str | None = None
    observed_at: datetime | None = Field(
        default=None,
        description="When this price was collected. A consumer cannot otherwise tell "
        "a price from this morning from one from March.",
    )


class CanonicalProductDetail(BaseModel):
    canonical_product_id: str
    brand: str | None = None
    manufacturer_sku: str | None = None
    canonical_name: str
    family: Family | None = None
    firing_range: FiringRange | None = None
    offers: list[Offer] = Field(default_factory=list)


class SearchResponse(BaseModel):
    products: list[CanonicalProduct]
    next_cursor: str | None = Field(
        default=None,
        description="Pass as `cursor` to continue. Absent when there is no more.",
    )


class BatchResponse(BaseModel):
    products: list[CanonicalProductDetail]


class Manufacturer(BaseModel):
    id: str
    name: str
    homepage_url: str | None = None
    product_count: int


class ManufacturersResponse(BaseModel):
    manufacturers: list[Manufacturer]


class Health(BaseModel):
    status: Literal["ok", "unavailable"]
