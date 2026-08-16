"""The registry the `catalogue.openapi.json` document is generated from.

Never hand-edit the generated document. Change this, run `make openapi`, and
commit the diff — which is what makes an API change visible in review.
"""

from __future__ import annotations

from mb_ceramics_catalogue.contracts import Operation, Parameter, Registry

from catalogue_service import __version__
from catalogue_service.contracts import (
    BatchResponse,
    CanonicalProductDetail,
    Health,
    ManufacturersResponse,
    SearchResponse,
)

DESCRIPTION = """
Cross-tenant reference data for ceramic materials: glazes, underglazes, engobes,
clay bodies, oxides and raw materials, collected from public supplier
catalogues and grouped into canonical products.

Read-only, and that is a contract rather than a convention — the build asserts
this document contains no operation other than `get`. Nothing a consumer does
can change what another consumer sees.

Prices are converted to EUR at a stated reference rate; every offer carries the
date its price was collected.
""".strip()


def registry() -> Registry:
    api = Registry(
        title="Ceramics catalogue",
        version=__version__,
        description=DESCRIPTION,
        servers=[{"url": "/", "description": "the service itself"}],
    )

    api.add(
        Operation(
            method="get",
            path="/health",
            operation_id="health",
            summary="Liveness, and whether the database is reachable",
            response=Health,
            errors=(503,),
            tags=("service",),
        )
    )

    api.add(
        Operation(
            method="get",
            path="/metrics",
            operation_id="metrics",
            summary="Prometheus metrics",
            media_type="text/plain",
            errors=(500,),
            tags=("service",),
        )
    )

    api.add(
        Operation(
            method="get",
            path="/v1/canonical-products",
            operation_id="searchCanonicalProducts",
            summary="Search canonical products",
            description=(
                "Aggregates only: the number of shops carrying each product and its "
                "price range. Offers are not included, because a search that returned "
                "every pack of every result would be answering a question nobody asked "
                "— fetch one product, or a batch, for those.\n\n"
                "Ordered by how many shops carry a product, then brand and code: a code "
                "eleven shops sell is more likely the one somebody means."
            ),
            tags=("catalogue",),
            parameters=(
                Parameter("q", description="Free text over name, brand and code."),
                Parameter(
                    "barcode",
                    description="Exact valid GTIN-8/12/13/14, compared as canonical GTIN-14.",
                ),
                Parameter("manufacturer", description="Restrict to one manufacturer id."),
                Parameter("family", description="Restrict to one product family."),
                Parameter(
                    "limit",
                    description="Results per page, capped at 200.",
                    schema={"type": "integer", "minimum": 1, "maximum": 200, "default": 25},
                ),
                Parameter(
                    "cursor",
                    description=(
                        "Continue from a previous page's `next_cursor`. The old API "
                        "capped `limit` and returned no cursor, so there was no way to "
                        "read past the cap at all."
                    ),
                ),
            ),
            response=SearchResponse,
            errors=(400, 503),
        )
    )

    api.add(
        Operation(
            method="get",
            path="/v1/canonical-products/{id}",
            operation_id="getCanonicalProduct",
            summary="One canonical product, with every shop's offer",
            tags=("catalogue",),
            parameters=(Parameter("id", location="path", description="A canonical product uuid."),),
            response=CanonicalProductDetail,
            errors=(400, 404, 503),
        )
    )

    api.add(
        Operation(
            method="get",
            path="/v1/canonical-products:batch",
            operation_id="batchCanonicalProducts",
            summary="Several canonical products, with their offers",
            description=(
                "The batch form of the operation above, for a client that has a list of "
                "ids and wants one round trip rather than fifty."
            ),
            tags=("catalogue",),
            parameters=(
                Parameter(
                    "ids",
                    required=True,
                    description="Comma-separated canonical product uuids, at most 200.",
                ),
            ),
            response=BatchResponse,
            errors=(400, 503),
        )
    )

    api.add(
        Operation(
            method="get",
            path="/v1/manufacturers",
            operation_id="listManufacturers",
            summary="Manufacturers with at least one active product",
            tags=("catalogue",),
            response=ManufacturersResponse,
            errors=(503,),
        )
    )

    return api
