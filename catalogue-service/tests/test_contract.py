"""The generated contract, and the properties it is generated to guarantee.

The read-only assertion is the important one. `catalogue-service` promised
read-only in a docstring, and nothing enforced it — a `POST` added in a hurry
would have broken the promise silently. It is now a test, so the property is
stronger after moving onto a framework than it was before (§10.2).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mb_ceramics_catalogue.contracts import assert_read_only

from catalogue_service.generate import DEFAULT_TARGET
from catalogue_service.spec import registry


@pytest.fixture(scope="module")
def document() -> dict:
    return registry().build()


def test_the_read_api_exposes_no_write_path(document):
    """Not a docstring any more. If someone adds a POST, this fails."""
    assert assert_read_only(document) == []


def test_the_checked_in_document_is_up_to_date(document):
    """Otherwise "regenerate it later" becomes "the spec is six months old"."""
    assert DEFAULT_TARGET.exists(), "run `make openapi` and commit catalogue.openapi.json"
    problems = registry().check(DEFAULT_TARGET)
    assert problems == [], "run `make openapi` and commit the result"


def test_it_is_openapi_31(document):
    """3.0's `nullable: true` cannot express a union, and this catalogue is
    mostly nullable fields."""
    assert document["openapi"].startswith("3.1")


class TestShapeCorrections:
    """Each of these was something the old API did implicitly (§10.3)."""

    def test_search_and_fetch_are_separate_operations(self, document):
        """`?ids=` and `?q=` returned different shapes from one operation id,
        which OpenAPI can only express as a union every client discriminates."""
        paths = document["paths"]
        assert "/v1/canonical-products" in paths
        assert "/v1/canonical-products/{id}" in paths
        assert "/v1/canonical-products:batch" in paths

        ids = {entry["get"]["operationId"] for entry in paths.values()}
        assert {"searchCanonicalProducts", "getCanonicalProduct", "batchCanonicalProducts"} <= ids

    def test_every_operation_documents_problem_json(self, document):
        """One error schema referenced everywhere, rather than an undocumented
        `{"error": "..."}` string."""
        for path, entry in document["paths"].items():
            for method, operation in entry.items():
                errors = [code for code in operation["responses"] if code.startswith(("4", "5"))]
                assert errors, f"{method.upper()} {path} documents no error response"
                for code in errors:
                    media = operation["responses"][code]["content"]
                    assert "application/problem+json" in media, f"{method.upper()} {path} {code}"

    def test_search_offers_a_cursor(self, document):
        """`limit` was capped at 200 and no cursor was returned, so there was no
        way to read past the cap at all."""
        search = document["paths"]["/v1/canonical-products"]["get"]
        assert any(p["name"] == "cursor" for p in search["parameters"])
        assert "next_cursor" in document["components"]["schemas"]["SearchResponse"]["properties"]

    def test_search_documents_exact_barcode_lookup(self, document):
        search = document["paths"]["/v1/canonical-products"]["get"]
        assert any(parameter["name"] == "barcode" for parameter in search["parameters"])

    def test_a_price_range_states_its_currency_and_rate_date(self, document):
        """It was a bare number over a mixture of EUR, USD and GBP, which made
        "cheapest per litre" quietly wrong for anything sold in two countries."""
        price = document["components"]["schemas"]["PriceSummary"]["properties"]
        assert "currency" in price
        assert "rate_date" in price

    def test_every_offer_carries_when_it_was_collected(self, document):
        """The fetch path had `observed_at` and the search path did not, so a
        consumer could not tell this morning's price from March's."""
        assert "observed_at" in document["components"]["schemas"]["Offer"]["properties"]

class TestGeneration:
    def test_generating_twice_produces_the_same_bytes(self, tmp_path: Path):
        """Otherwise regenerating makes a diff every time and people stop
        reading them."""
        target = tmp_path / "spec.json"
        assert registry().write(target) is True
        assert registry().write(target) is False

    def test_drift_is_reported_by_name(self, tmp_path: Path):
        target = tmp_path / "spec.json"
        registry().write(target)

        document = json.loads(target.read_text())
        del document["paths"]["/v1/manufacturers"]
        target.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")

        problems = registry().check(target)
        assert any("/v1/manufacturers" in problem for problem in problems)

    def test_a_missing_document_is_reported_rather_than_crashing(self, tmp_path: Path):
        problems = registry().check(tmp_path / "absent.json")
        assert problems and "does not exist" in problems[0]
