"""The typed source configuration, and the promise it makes to the scrapers.

The load-bearing test here is `test_the_projection_is_the_raw_entry_plus_nothing_surprising`.
Introducing this model silently changed two scrapers' behaviour, because pydantic
filled in `variant_combinations=False` and `use_advertised_sitemaps=False` for
every source that had not set them, and both are read as
`config.get(key, True)`. `ceram-decor` went from 49 records to 40.

The golden files caught that. This test catches it a hundred times faster, and
without a 638 MB cache checked out.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from mb_ceramics_catalogue import scrapers
from mb_ceramics_catalogue.config.source_projection import inspect_sources, project_legacy_source
from mb_ceramics_catalogue.config.sources import SourceConfig, SourcesFile, default_path

MINIMAL = {"label": "Test", "url": "https://example.test/", "scraper": "shopify"}


@pytest.fixture(scope="module")
def raw() -> dict:
    return json.loads(default_path().read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def parsed() -> SourcesFile:
    return SourcesFile.load(default_path())


def test_the_real_sources_file_validates(parsed: SourcesFile):
    assert len(parsed) >= 20
    for name, config in parsed.items():
        assert config.scraper in scrapers.REGISTRY, name


def test_typed_projection_golden_covers_every_checked_in_source(parsed: SourcesFile):
    reports = inspect_sources(parsed)
    assert [report.source.source_id for report in reports] == sorted(parsed.names())
    encoded = json.dumps(
        [report.model_dump(mode="json") for report in reports],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert hashlib.sha256(encoded).hexdigest() == (
        "9706610cfc5da69112b6d18443d48823e0f8bb51b1bf7f58ca1c3166ea5efbb6"
    )


def test_shopify_projection_separates_connector_browser_and_datasets():
    report = project_legacy_source(
        "shop", SourceConfig(**MINIMAL, collections=["clay"], render=True)
    )
    assert report.source.connector.kind == "shopify"
    assert report.source.browser.kind == "camoufox"
    assert {item.dataset for item in report.source.datasets} == {
        "ceramics.catalogue_item.v2",
    }
    assert "commerce.stock_observation.v1" in report.source.available_datasets


def test_typed_projection_preserves_source_semantics_for_every_entry(parsed: SourcesFile):
    """Every non-connector concern survives in its typed owner losslessly."""
    for name, legacy in parsed.items():
        report = project_legacy_source(name, legacy)
        typed = report.source
        assert (typed.label, typed.url, typed.country, typed.note) == (
            legacy.label, legacy.url, legacy.country, legacy.note
        )
        assert typed.crawl.model_dump() == {
            "delay": legacy.delay,
            "timeout_seconds": legacy.timeout_seconds,
            "product_concurrency": legacy.product_concurrency,
            "ignore_robots": legacy.ignore_robots,
            "obey_robots": legacy.obey_robots,
            "render": legacy.render,
            "proxy_eligible": legacy.proxy_eligible,
            "proxy_policy": legacy.proxy_policy,
            "proxy_profile": legacy.proxy_profile,
            "proxy_country": legacy.proxy_country,
            "proxy_session_minutes": legacy.proxy_session_minutes,
            "proxy_max_megabytes": legacy.proxy_max_megabytes,
            "proxy_pilot": legacy.proxy_pilot,
        }
        ceramics = typed.datasets[0]
        assert ceramics.kind == "ceramics"
        assert ceramics.scope == legacy.scope
        assert ceramics.enrichments == tuple(legacy.enrichments or ())
        assert ceramics.brand == legacy.brand
        assert ceramics.is_manufacturer == legacy.is_manufacturer
        assert ceramics.material_categories == tuple(legacy.material_categories or ())
        assert ceramics.excluded_categories == tuple(legacy.excluded_categories or ())
        assert ceramics.currency == legacy.currency
        assert ceramics.vat_status == legacy.vat_status
        assert ceramics.vat_rate == legacy.vat_rate
        assert not report.ambiguous_fields
        expected_unused = {
            "amaco": ("sitemaps",),
            "keramik-kraft": ("product_pattern",),
        }
        assert report.unused_fields == expected_unused.get(name, ())
        if typed.connector.kind == "shopify":
            assert typed.connector.collections == tuple(legacy.collections or ())
            assert typed.connector.page_limit == (legacy.page_limit or 200)
        elif typed.connector.kind == "woocommerce":
            assert typed.connector.store_categories == tuple(legacy.store_categories or ())
            assert typed.connector.page_limit == (legacy.page_limit or 100)
        elif typed.connector.kind == "bigcommerce":
            assert typed.connector.token_page == legacy.category_url
            assert typed.connector.page_limit == (legacy.page_limit or 200)
        elif typed.connector.kind == "wix":
            assert typed.connector.sitemaps == tuple(legacy.sitemaps or ())
            assert typed.connector.product_pattern == legacy.product_pattern
        elif typed.connector.kind in {
            "shopware", "sumup", "starweb", "nitrosell", "prestashop", "sio2", "pagecommerce"
        }:
            assert typed.connector.category_urls == tuple(legacy.category_urls or ())
            assert typed.connector.product_pattern == legacy.product_pattern
            assert typed.connector.page_limit == (legacy.page_limit or 500)
        elif typed.connector.kind in {"axner", "ceramicolours", "keramik_kraft"}:
            assert typed.connector.page_limit == (legacy.page_limit or 500)
        else:
            assert typed.connector.kind == "legacy"
            raw = legacy.as_scraper_config()
            expected = {
                key: raw[key]
                for key in sorted(legacy.model_fields_set)
                if key in raw and key in {
                    "sitemaps", "use_advertised_sitemaps", "product_pattern",
                    "pagination_patterns", "page_limit", "category_url", "category_urls",
                    "category_ids", "category_paths", "category_page_limit", "collections",
                    "store_categories", "card_links_only", "enrich_product_pages",
                    "variation_page_limit", "variant_combinations",
                    "stock_from_add_to_cart_maximum", "stock_from_quantity_maximum",
                    "inventory_product_json", "inventory_product_html", "inventory_section_id",
                    "inventory_prefilter_materials",
                }
            }
            assert typed.connector.options == expected


def test_the_projection_is_the_raw_entry_plus_nothing_surprising(raw: dict, parsed: SourcesFile):
    """A scraper must see exactly the keys the file gave it.

    A key that appears out of nowhere with a falsy value is not harmless: any
    reader doing `config.get(key, True)` silently flips. Only the three fields
    with deliberate concrete defaults may be added, and they may only ever be
    added as the same falsy value a missing key already reads as.
    """
    allowed_additions = {"scope": "materials", "ignore_robots": False, "is_manufacturer": False}
    operator_only = {
        "proxy_policy",
        "proxy_eligible",
        "proxy_profile",
        "proxy_country",
        "proxy_session_minutes",
        "proxy_max_megabytes",
        "proxy_pilot",
    }

    for name, config in parsed.items():
        projected = config.as_scraper_config()
        entry = raw[name]

        for key, value in entry.items():
            if key in operator_only:
                assert key not in projected, f"{name}: projection leaked operator-only {key!r}"
                continue
            assert key in projected, f"{name}: projection dropped {key!r}"
            assert projected[key] == value, f"{name}: projection changed {key!r}"

        for key, value in projected.items():
            if key in entry:
                continue
            assert key in allowed_additions, f"{name}: projection invented {key!r}={value!r}"
            assert value == allowed_additions[key], f"{name}: {key!r} defaulted to {value!r}"


def test_no_scraper_defaults_a_projected_key_to_something_truthy(parsed: SourcesFile):
    """Guard the rule the projection depends on, at its other end.

    If someone adds `config.get("scope", "all")` to a scraper, the projection's
    concrete `scope` default starts overriding it. This asserts the three
    always-projected keys are never read with a default that differs from what
    they are projected as.
    """
    import inspect
    import re

    from mb_ceramics_catalogue import scrapers as package

    always_projected = {"scope": "'materials'", "ignore_robots": None, "is_manufacturer": None}
    pattern = re.compile(r"""config\.get\(\s*["'](\w+)["']\s*,\s*([^)]+)\)""")

    for scraper_name in package.REGISTRY:
        module = inspect.getmodule(package.load(scraper_name))
        assert module is not None
        for key, expected in pattern.findall(inspect.getsource(module)):
            if key not in always_projected:
                continue
            wanted = always_projected[key]
            assert wanted is not None and expected.strip() == wanted, (
                f"{scraper_name} reads {key!r} with default {expected.strip()}, "
                f"which the projection would override"
            )


def test_an_unknown_key_is_rejected_by_name():
    """The whole point: `store_category` must not be silently ignored."""
    with pytest.raises(ValidationError) as caught:
        SourceConfig(**MINIMAL, store_category=["emaux"])
    assert "store_category" in str(caught.value)


def test_an_unknown_scraper_names_the_known_ones():
    with pytest.raises(ValidationError) as caught:
        SourceConfig(**{**MINIMAL, "scraper": "shopfiy"})
    message = str(caught.value)
    assert "shopfiy" in message
    assert "shopify" in message


def test_a_non_https_url_is_rejected():
    with pytest.raises(ValidationError):
        SourceConfig(**{**MINIMAL, "url": "http://example.test/"})


class TestRobotsMayOnlyBeIgnoredDeliberately:
    """Was a test over sources.json; is now a rule the model enforces.

    That matters because a source created through `PUT /v1/sources/{id}` is held
    to it too, and a test over a checked-in file never could be.
    """

    def test_a_note_is_required(self):
        with pytest.raises(ValidationError, match="no note says why"):
            SourceConfig(**MINIMAL, ignore_robots=True, delay=5.0)

    def test_a_slow_rate_is_required(self):
        with pytest.raises(ValidationError, match="crawled slowly"):
            SourceConfig(**MINIMAL, ignore_robots=True, note="operator decision", delay=0.5)

    def test_both_together_are_accepted(self):
        config = SourceConfig(**MINIMAL, ignore_robots=True, note="operator decision", delay=2.0)
        assert config.ignore_robots

    def test_the_real_file_obeys_it(self, parsed: SourcesFile):
        # Redundant with the model, and kept: it is the assertion that the
        # checked-in data has not drifted, rather than that the rule exists.
        for name, config in parsed.items():
            if config.ignore_robots:
                assert config.note, name
                assert (config.delay or 0) >= 2.0, name


class TestEnrichments:
    """Derived fields are opt-in, and a typo in the selection is not silent."""

    def test_an_unknown_module_names_itself(self):
        with pytest.raises(ValidationError, match="glazes"):
            SourceConfig(**{**MINIMAL, "enrichments": ["glazes"]})

    def test_a_bundle_is_accepted(self):
        assert SourceConfig(**{**MINIMAL, "enrichments": ["ceramic-materials"]}).enrichments

    def test_the_projection_carries_the_selection(self):
        config = SourceConfig(**{**MINIMAL, "enrichments": ["firing"]})
        assert config.as_scraper_config()["enrichments"] == ["firing"]
        # ...and an unset one stays absent, like every other optional key.
        assert "enrichments" not in SourceConfig(**MINIMAL).as_scraper_config()

    def test_every_materials_source_selects_its_enrichment(self, parsed: SourcesFile):
        """A materials shop with no selection collects prices with no ceramics.

        `classification` is added by scope regardless, so this is not about the
        dump being empty — it is that the file should say what it wants derived
        rather than leaning on that one implication.
        """
        for name, config in parsed.items():
            if config.scope == "materials":
                assert config.enrichments, f"{name} selects no enrichment"


class TestSelect:
    def test_all_returns_every_source_in_file_order(self, parsed: SourcesFile, raw: dict):
        assert parsed.select("all") == list(raw)

    def test_a_list_is_trimmed_and_respected(self, parsed: SourcesFile):
        assert parsed.select(" ceradel , mayco ") == ["ceradel", "mayco"]

    def test_an_unknown_name_lists_the_known_ones(self, parsed: SourcesFile):
        with pytest.raises(ValueError, match="unknown source") as caught:
            parsed.select("ceradel,cerardel")
        assert "cerardel" in str(caught.value)
        assert "Known:" in str(caught.value)
