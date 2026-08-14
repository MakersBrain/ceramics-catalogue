"""What a source is, as a validated object rather than a bag of strings.

`sources.json` is eighty entries with roughly thirty optional keys, and every
scraper read it as `config.get("...")` against `dict[str, Any]`. A typo in a key
was silent in the worst possible way: write `store_category` instead of
`store_categories` and the source quietly crawls the shop's entire catalogue
instead of the six allowlisted departments, produces ten thousand rows of
kilns and brushes, and reports success.

`extra="forbid"` is therefore the single most valuable line in this module. It
turns that class of mistake from a data-quality incident discovered weeks later
into a startup error naming the source and the field.

The models are also what `POST /v1/runs` validates a run's parameters against
(§6), so the CLI, the API and the scheduler agree on what a valid run is by
construction rather than by three separate `.get()` calls.
"""

from __future__ import annotations

import json
from collections.abc import ItemsView, Iterator
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator

#: Where a record's price stands in relation to VAT. Anything else is a typo:
#: reading an unknown value as "not stated" would silently compare a gross price
#: against a net one, which is the one comparison this catalogue exists to make.
VatStatus = Literal["inclusive", "exclusive", "unknown"]

#: Whether a source is crawled for ceramic materials only, or in full.
Scope = Literal["materials", "all"]
ProxyPolicy = Literal["never", "fallback", "always"]


class SourceConfig(BaseModel):
    """One supplier's entry in sources.json."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # -- identity ---------------------------------------------------------
    label: str
    url: str
    scraper: str
    scope: Scope = "materials"
    country: str | None = None
    #: Free prose, for the operator. Required when robots.txt is ignored.
    note: str | None = None
    #: The maker whose products this shop sells, where it sells only one.
    brand: str | None = None
    #: True only for a manufacturer's own shop, where a bare article number is
    #: the manufacturer's code rather than a retailer's shelf reference.
    is_manufacturer: bool = False

    # -- fetch policy -----------------------------------------------------
    #: A hard floor on the gap between requests to this host, in seconds.
    delay: float | None = Field(default=None, ge=0)
    #: Deliberate, and only with a `note` saying why and a `delay` of at least 2s.
    ignore_robots: bool = False
    #: Obey robots.txt Disallow for this source even when the run ignores it.
    #:
    #: The run-level default is `robots=ignore`, which is a policy about the
    #: fleet. This is the other direction and belongs to the source: a shop we
    #: want to stay on good terms with, or one that has already objected, is
    #: crawled by its own rules whatever the fleet does.
    #: `None`, not `False`: an unset optional must be absent from
    #: `as_scraper_config`, not present and falsy — see that method's docstring.
    obey_robots: bool | None = None
    #: Force the browser renderer for a source whose pages are built client-side.
    render: bool | None = None
    product_concurrency: int | None = Field(default=None, ge=1)
    #: How long one source may run before the crawl gives up on it. `None` takes
    #: the run's default. There was no per-source deadline at all before: a slow
    #: origin held its slot for ever, and the 03:00 run was still going at 09:00.
    timeout_seconds: float | None = Field(default=None, gt=0)
    #: Residential transport is an operator-owned compatibility exception.
    #: A profile is a logical name resolved from a mounted secret, never a URL.
    proxy_policy: ProxyPolicy = "never"
    proxy_profile: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    proxy_country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    proxy_session_minutes: int = Field(default=30, ge=1, le=1440)
    proxy_max_megabytes: int = Field(default=25, ge=1, le=300)
    proxy_pilot: bool = False

    # -- discovery --------------------------------------------------------
    sitemaps: list[str] | None = None
    use_advertised_sitemaps: bool | None = None
    product_pattern: str | None = None
    pagination_patterns: list[str] | None = None
    page_limit: int | None = Field(default=None, ge=1)
    category_url: str | None = None
    category_urls: list[str] | None = None
    category_ids: list[int | str] | None = None
    category_paths: list[str] | None = None
    category_page_limit: int | None = Field(default=None, ge=1)
    collections: list[str] | None = None
    store_categories: list[str] | None = None
    card_links_only: bool | None = None
    enrich_product_pages: bool | None = None
    variation_page_limit: int | None = Field(default=None, ge=1)
    variant_combinations: bool | None = None

    # -- scope filtering --------------------------------------------------
    material_categories: list[str] | None = None
    excluded_categories: list[str] | None = None
    #: A manufacturer catalogue that publishes specifications but no price.
    identity_only: bool | None = None

    # -- pricing ----------------------------------------------------------
    vat_status: VatStatus | None = None
    vat_rate: float | None = Field(default=None, ge=0, le=1)
    currency: str | None = None

    @field_validator("url")
    @classmethod
    def _https_only(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError(f"must be an https URL, got {value!r}")
        return value

    @field_validator("scraper")
    @classmethod
    def _known_scraper(cls, value: str) -> str:
        # Imported here rather than at module scope: `scrapers/__init__` is
        # cheap, but config should not drag the collection package in for
        # anything that only wants to read a settings file.
        from mb_ceramics_catalogue import scrapers

        if value not in scrapers.REGISTRY:
            known = ", ".join(sorted(scrapers.REGISTRY))
            raise ValueError(f"unknown scraper {value!r}; known: {known}")
        return value

    @model_validator(mode="after")
    def _robots_may_only_be_ignored_deliberately(self) -> SourceConfig:
        """Ignoring robots.txt costs a note and a slow rate, in that order.

        This was a unit test asserting over sources.json. Making it a model rule
        means a source added through the API is held to it too, which the test
        never could be.
        """
        if not self.ignore_robots:
            return self
        if not self.note:
            raise ValueError("ignore_robots is set but no note says why")
        if (self.delay or 0) < 2.0:
            raise ValueError(
                f"ignore_robots is set but delay is {self.delay or 0}; "
                "a source crawled against its published rules must be crawled slowly"
            )
        return self

    @model_validator(mode="after")
    def _proxy_policy_is_safe(self) -> SourceConfig:
        if self.proxy_policy != "never" and not self.proxy_profile:
            raise ValueError("proxy policy requires a logical proxy_profile")
        return self

    def as_scraper_config(self) -> dict[str, Any]:
        """The plain dict the scrapers still read.

        The 4,700 lines of collection take `dict[str, Any]` and are not being
        rewritten, so validation happens here and the scrapers keep the shape
        they already have.

        **An unset optional key must be absent from this dict, not present and
        falsy.** That is not tidiness, it is correctness, and getting it wrong
        broke two scrapers: `prestashop` reads
        `config.get("variant_combinations", True)` and `pagecrawl` reads
        `config.get("use_advertised_sitemaps", True)`. A projection that emitted
        `False` for every source that had not set them flipped both defaults
        from on to off, and `ceram-decor` quietly dropped from 49 records to 40.
        The golden files caught it; nothing else would have.

        So every optional field defaults to `None` and `exclude_none` drops it,
        which makes this dict identical to the raw JSON entry. The three
        exceptions — `scope`, `ignore_robots`, `is_manufacturer` — have concrete
        defaults because typed callers read them, and each was checked against
        every `config.get(key, default)` in the scrapers to confirm no reader
        defaults them to anything but the same falsy value.
        """
        return self.model_dump(
            exclude_none=True,
            exclude={
                "proxy_policy",
                "proxy_profile",
                "proxy_country",
                "proxy_session_minutes",
                "proxy_max_megabytes",
                "proxy_pilot",
            },
        )


class SourcesFile(RootModel[dict[str, SourceConfig]]):
    """The whole of sources.json, keyed by source id."""

    root: dict[str, SourceConfig]

    def __iter__(self) -> Iterator[str]:  # type: ignore[override]
        return iter(self.root)

    def __getitem__(self, name: str) -> SourceConfig:
        return self.root[name]

    def __contains__(self, name: str) -> bool:
        return name in self.root

    def get(self, name: str) -> SourceConfig | None:
        """The config for a source, or None if this build has never heard of it.

        A worker can be handed a job for a source that has since been removed
        from the file — the queue outlives a deploy. Asking with this rather
        than indexing keeps that a job that fails where failures are recorded.
        """
        return self.root.get(name)

    def __len__(self) -> int:
        return len(self.root)

    def items(self) -> ItemsView[str, SourceConfig]:
        return self.root.items()

    def names(self) -> list[str]:
        return list(self.root)

    def select(self, expression: str) -> list[str]:
        """Resolve 'all' or a comma-separated list into source ids.

        Raises with the unknown names *and* the known ones, because the usual
        reason to be here is a typo and the useful reply is the list to compare
        against.
        """
        if expression.strip() == "all":
            return self.names()
        wanted = [name.strip() for name in expression.split(",") if name.strip()]
        if unknown := [name for name in wanted if name not in self.root]:
            raise ValueError(
                f"unknown source(s): {', '.join(unknown)}. Known: {', '.join(sorted(self.root))}"
            )
        return wanted

    def as_scraper_configs(self) -> dict[str, dict[str, Any]]:
        return {name: source.as_scraper_config() for name, source in self.root.items()}

    @classmethod
    def load(cls, path: Path) -> SourcesFile:
        """Read and validate a sources file, naming the file in any error."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError(f"{path} is not valid JSON: {error}") from error
        return cls.model_validate(raw)


def default_path() -> Path:
    """Where sources.json lives, whether installed or run from the checkout.

    The wheel carries it as package data (see `force-include` in
    pyproject.toml); a development checkout has it beside the project root. A
    worker in an image only ever has the first, and `python3 dump.py` in the
    repository only ever had the second.
    """
    packaged = Path(__file__).resolve().parent.parent / "data" / "sources.json"
    if packaged.exists():
        return packaged
    return Path(__file__).resolve().parents[3] / "sources.json"
