from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import httpx
import pytest

from mb_ceramics_catalogue.connectors import (
    CollectionRequest,
    ConnectorCheckpoint,
    DiagnosticCode,
    RefreshMode,
    SnapshotField,
    StockQuantityKind,
)
from mb_ceramics_catalogue.connectors.bigcommerce import (
    BigCommerceConnector,
    BigCommerceOptions,
)
from mb_ceramics_catalogue.pipeline.budget import RequestBudget, RequestCost

NOW = datetime(2026, 8, 15, tzinfo=UTC)


def token(origin: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"cors": [origin]}).encode()).decode().rstrip("=")
    return f"{'a' * 20}.{payload}.{'b' * 24}"


class Transport:
    def __init__(self, documents, payloads):
        self.documents = dict(documents)
        self.payloads = list(payloads)
        self.document_calls = []
        self.json_calls = []

    async def document(self, url, *, rendered=False):
        self.document_calls.append((url, rendered))
        value = self.documents[(url, rendered)]
        if isinstance(value, Exception):
            raise value
        return value

    async def request_json(self, url, *, headers, body, browser_context_url=None):
        self.json_calls.append((url, headers, body, browser_context_url))
        value = self.payloads.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def request(*, limit: int | None = None) -> CollectionRequest:
    return CollectionRequest(
        source_id="shop",
        base_url="https://shop.test/path",
        refresh_mode=RefreshMode.FULL,
        requested_fields=frozenset(SnapshotField),
        result_limit=limit,
    )


def node(identifier: int = 10) -> dict:
    return {
        "entityId": identifier,
        "name": "Transparent Glaze",
        "path": f"/glaze-{identifier}/",
        "sku": "PARENT",
        "description": "A gloss glaze.",
        "brand": {"name": "Test Ceramics"},
        "availabilityV2": {"status": "Available"},
        "defaultImage": {"urlOriginal": "https://cdn.test/default.jpg"},
        "images": {"edges": [{"node": {"urlOriginal": "https://cdn.test/image.jpg"}}]},
        "prices": {
            "price": {"value": 12.5, "currencyCode": "EUR"},
            "retailPrice": {"value": 15},
        },
        "categories": {"edges": [{"node": {"name": "Glazes", "path": "/glazes"}}]},
        "customFields": {
            "edges": [
                {"node": {"name": "Finish", "value": "Gloss"}},
                {"node": {"name": "SDS", "value": "/docs/sds.pdf"}},
            ]
        },
        "variants": {
            "edges": [
                {
                    "node": {
                        "entityId": 101,
                        "sku": "GL-500",
                        "defaultImage": {"urlOriginal": "https://cdn.test/variant.jpg"},
                        "prices": {"price": {"value": 11, "currencyCode": "EUR"}},
                        "inventory": {"isInStock": True, "aggregated": {"availableToSell": 7}},
                        "options": {
                            "edges": [
                                {
                                    "node": {
                                        "displayName": "Size",
                                        "values": {"edges": [{"node": {"label": "500 ml"}}]},
                                    }
                                }
                            ]
                        },
                    }
                }
            ]
        },
    }


def payload(nodes, *, has_next=False, cursor=None):
    return {
        "data": {
            "site": {
                "products": {
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                    "edges": [{"node": item} for item in nodes],
                }
            }
        }
    }


@pytest.mark.asyncio
async def test_request_budget_exhaustion_after_token_discovery_is_typed_and_resumable() -> None:
    secret = token("https://shop.test")
    transport = Transport(
        {("https://shop.test/path", False): f'local_token = "{secret}"'},
        [],
    )
    budget = RequestBudget(RequestCost(http_requests=1))
    connector = BigCommerceConnector(transport, budget=budget)

    [page] = [item async for item in connector.collect(request())]

    assert page.terminal and not page.enumeration_intact
    assert page.resume_after == {"after": None, "sequence": 0}
    assert page.diagnostics[0].code == DiagnosticCode.REQUEST_BUDGET_EXHAUSTED
    assert budget.used.http_requests == 1
    assert transport.json_calls == []


@pytest.mark.asyncio
async def test_result_limit_is_incomplete_and_keeps_graphql_cursor() -> None:
    secret = token("https://shop.test")
    transport = Transport(
        {("https://shop.test/path", False): f'local_token = "{secret}"'},
        [payload([node()], has_next=True, cursor="cursor-1")],
    )
    connector = BigCommerceConnector(
        transport,
        BigCommerceOptions(allow_rendered_token_fallback=False),
        clock=lambda: NOW,
    )

    [page] = [item async for item in connector.collect(request(limit=1))]

    assert page.terminal and not page.enumeration_intact
    assert page.resume_after == {"after": "cursor-1", "sequence": 1}
    assert page.diagnostics[0].code == DiagnosticCode.RESULT_LIMIT_REACHED
    assert transport.json_calls[0][2]["variables"]["first"] == 1


@pytest.mark.asyncio
async def test_cursor_pages_emit_neutral_variants_without_leaking_token() -> None:
    secret = token("https://shop.test")
    transport = Transport(
        {("https://shop.test/path", False): f'local_token = "{secret}"'},
        [payload([node()], has_next=True, cursor="cursor-1"), payload([], has_next=False)],
    )
    connector = BigCommerceConnector(
        transport,
        BigCommerceOptions(allow_rendered_token_fallback=False),
        clock=lambda: NOW,
    )

    pages = [page async for page in connector.collect(request())]

    assert len(pages) == 2 and pages[-1].terminal
    assert pages[0].resume_after == {"after": "cursor-1", "sequence": 1}
    snapshot = pages[0].items[0]
    variant = snapshot.variants[0]
    assert variant.title == "500 ml"
    assert variant.offers[0].price.amount == 11
    assert variant.stock is not None and variant.stock.quantity == 7
    assert variant.stock.quantity_kind == StockQuantityKind.EXACT
    assert snapshot.documents[0].url == "https://shop.test/docs/sds.pdf"
    serialized = pages[0].model_dump_json()
    assert secret not in serialized
    assert secret not in json.dumps(pages[0].resume_after)


@pytest.mark.asyncio
async def test_checkpoint_resumes_with_cursor_but_rediscovers_ephemeral_token() -> None:
    secret = token("https://shop.test")
    transport = Transport(
        {("https://shop.test/path", False): f'storefront_api_token: "{secret}"'},
        [payload([], has_next=False)],
    )
    connector = BigCommerceConnector(
        transport,
        BigCommerceOptions(allow_rendered_token_fallback=False),
        clock=lambda: NOW,
    )
    checkpoint = ConnectorCheckpoint(
        connector="bigcommerce",
        connector_version="1",
        source_id="shop",
        lineage="lineage",
        resume_after={"after": "opaque-cursor", "sequence": 4},
    )

    [page] = [item async for item in connector.collect(request(), checkpoint)]

    assert page.sequence == 4
    assert transport.json_calls[0][2]["variables"]["after"] == "opaque-cursor"
    assert transport.document_calls  # token was rediscovered, not checkpointed


@pytest.mark.asyncio
async def test_wrong_origin_token_is_rejected_and_rendered_fallback_is_backend_neutral() -> None:
    wrong = token("https://localhost")
    right = token("https://shop.test")
    transport = Transport(
        {
            ("https://shop.test/path", False): f'local_token="{wrong}"',
            ("https://shop.test/path", True): f'local_token="{right}"',
        },
        [payload([], has_next=False)],
    )
    connector = BigCommerceConnector(transport, clock=lambda: NOW)

    [page] = [item async for item in connector.collect(request())]

    assert page.terminal and page.enumeration_intact
    assert transport.json_calls[0][3] == "https://shop.test/path"
    assert transport.document_calls == [
        ("https://shop.test/path", False),
        ("https://shop.test/path", True),
    ]


@pytest.mark.asyncio
async def test_non_object_token_claims_are_rejected_without_crashing() -> None:
    claims = base64.urlsafe_b64encode(json.dumps(["https://shop.test"]).encode()).decode().rstrip("=")
    malformed = f"{'a' * 20}.{claims}.{'b' * 24}"
    transport = Transport(
        {
            ("https://shop.test/path", False): f'local_token="{malformed}"',
            ("https://shop.test", False): "",
        },
        [],
    )
    connector = BigCommerceConnector(
        transport,
        BigCommerceOptions(allow_rendered_token_fallback=False),
        clock=lambda: NOW,
    )

    [page] = [item async for item in connector.collect(request())]

    assert page.diagnostics[0].code == DiagnosticCode.PARSER_UNSUPPORTED
    assert malformed not in page.model_dump_json()


@pytest.mark.asyncio
async def test_errors_are_typed_and_never_include_token_or_remote_error_body() -> None:
    secret = token("https://shop.test")
    transport = Transport(
        {("https://shop.test/path", False): f'local_token="{secret}"'},
        [{"errors": [{"message": f"bad bearer {secret}"}]}],
    )
    connector = BigCommerceConnector(
        transport,
        BigCommerceOptions(allow_rendered_token_fallback=False),
        clock=lambda: NOW,
    )

    [page] = [item async for item in connector.collect(request())]

    assert not page.enumeration_intact
    assert page.diagnostics[0].code == DiagnosticCode.SCHEMA_CHANGED
    assert secret not in page.model_dump_json()


@pytest.mark.asyncio
async def test_transport_failure_is_resumable_without_secret_diagnostics() -> None:
    secret = token("https://shop.test")
    transport = Transport(
        {("https://shop.test/path", False): f'local_token="{secret}"'},
        [httpx.ReadTimeout(f"secret={secret}")],
    )
    connector = BigCommerceConnector(
        transport,
        BigCommerceOptions(allow_rendered_token_fallback=False),
        clock=lambda: NOW,
    )
    [page] = [item async for item in connector.collect(request())]
    assert page.diagnostics[0].code == DiagnosticCode.ENUMERATION_INCOMPLETE
    assert page.resume_after == {"after": None, "sequence": 0}
    assert secret not in page.model_dump_json()
