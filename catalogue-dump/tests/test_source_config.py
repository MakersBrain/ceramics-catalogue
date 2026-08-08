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

import json

import pytest
from pydantic import ValidationError

from ateliera_catalogue import scrapers
from ateliera_catalogue.config.sources import SourceConfig, SourcesFile, default_path

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


def test_the_projection_is_the_raw_entry_plus_nothing_surprising(raw: dict, parsed: SourcesFile):
    """A scraper must see exactly the keys the file gave it.

    A key that appears out of nowhere with a falsy value is not harmless: any
    reader doing `config.get(key, True)` silently flips. Only the three fields
    with deliberate concrete defaults may be added, and they may only ever be
    added as the same falsy value a missing key already reads as.
    """
    allowed_additions = {"scope": "materials", "ignore_robots": False, "is_manufacturer": False}

    for name, config in parsed.items():
        projected = config.as_scraper_config()
        entry = raw[name]

        for key, value in entry.items():
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

    from ateliera_catalogue import scrapers as package

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
