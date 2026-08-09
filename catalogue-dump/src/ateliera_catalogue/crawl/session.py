"""The collaborators a crawl needs, built and torn down as one thing.

`main()` used to construct the client, limiter, browser, cache and fetcher
inline and close the browser in a bare `finally`. That worked, but "the browser
must be closed" was a property of one function's control flow rather than of the
object, so the worker — which builds the same five things per job — would have
had to remember it too.

An `async with` makes it structural: leaving the block closes the browser and
the client whatever the reason for leaving, including a cancellation arriving
mid-fetch.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ateliera_catalogue.config.settings import CrawlParams
from ateliera_catalogue.observability import logging as obs
from ateliera_catalogue.scrapers.base import USER_AGENT, BrowserRenderer, Fetcher, HostLimiter
from ateliera_catalogue.scrapers.cache import ResponseCache

LOGGER = obs.get_logger("catalogue.session")

#: Seconds before one HTTP request is abandoned. Per request, not per source —
#: the per-source deadline is `CrawlParams.source_timeout_seconds`.
REQUEST_TIMEOUT = 30.0


@dataclass
class CrawlSession:
    """Everything a scraper is handed, plus the knobs a caller may still read."""

    client: httpx.AsyncClient
    limiter: HostLimiter
    browser: BrowserRenderer
    cache: ResponseCache
    fetcher: Fetcher

    def cache_summary(self) -> str:
        return self.cache.summary()


@asynccontextmanager
async def open_session(
    params: CrawlParams, cache_dir: Path | str | None = None
) -> AsyncIterator[CrawlSession]:
    """Build the fetch stack for one crawl and guarantee it is torn down."""
    cache = ResponseCache(
        cache_dir or ".",
        mode=params.cache_mode if cache_dir else "off",
        max_age=params.cache_max_age_seconds,
    )
    limiter = HostLimiter(params.delay, params.concurrency)
    browser = BrowserRenderer(params.browser != "never")

    async with httpx.AsyncClient(
        headers={"user-agent": USER_AGENT}, timeout=REQUEST_TIMEOUT, follow_redirects=True
    ) as client:
        fetcher = Fetcher(
            client, limiter, browser, params.browser, cache=cache,
            impersonate_policy=params.impersonate,
            robots_policy=params.robots,
        )
        session = CrawlSession(
            client=client, limiter=limiter, browser=browser, cache=cache, fetcher=fetcher
        )
        try:
            yield session
        finally:
            # Not conditional on how the block was left. A cancelled run that
            # leaves camoufox running leaks a browser process per attempt, and
            # a long-lived worker would accumulate one per cancelled job.
            try:
                await browser.close()
            except Exception:
                LOGGER.warning("session.browser_close_failed", exc_info=True)


def cache_directory(explicit: Path | str | None, default: Path) -> Path | None:
    """Resolve where recorded responses live, or None for no cache at all.

    Kept as a function because three entry points ask the same question and the
    answer has a sharp edge: passing `"."` with mode `off` (which is what the
    old code did) writes nothing but still builds a cache object rooted at the
    working directory, and one accidental mode change would scatter gzipped
    pages across the repository.
    """
    if explicit is None:
        return None
    resolved = Path(explicit)
    return resolved if resolved.is_absolute() else default.parent / resolved


def describe(params: CrawlParams) -> dict[str, Any]:
    """The run parameters worth putting on the run span and the first log line."""
    return {
        "sources_at_once": params.sources,
        "host_concurrency": params.concurrency,
        "delay": params.delay,
        "browser": params.browser,
        "impersonate": params.impersonate,
        "robots": params.robots,
        "cache_mode": params.cache_mode,
        "cache_max_age_hours": params.cache_max_age_hours,
        "limit": params.limit,
    }
