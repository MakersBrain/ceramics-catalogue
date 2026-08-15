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

from mb_ceramics_catalogue.config.settings import CrawlParams
from mb_ceramics_catalogue.observability import logging as obs
from mb_ceramics_catalogue.proxy import ProxyLease
from mb_ceramics_catalogue.scrapers.base import USER_AGENT, BrowserRenderer, Fetcher, HostLimiter
from mb_ceramics_catalogue.scrapers.cache import ResponseCache
from mb_ceramics_catalogue.transports.browser import BrowserBackend, BrowserJobContext, BrowserSession

LOGGER = obs.get_logger("catalogue.session")

#: Seconds before one HTTP request is abandoned. Per request, not per source —
#: the per-source deadline is `CrawlParams.source_timeout_seconds`.
REQUEST_TIMEOUT = 30.0


@dataclass
class CrawlSession:
    """Everything a scraper is handed, plus the knobs a caller may still read."""

    client: httpx.AsyncClient
    limiter: HostLimiter
    browser: BrowserSession
    cache: ResponseCache
    fetcher: Fetcher

    def cache_summary(self) -> str:
        return self.cache.summary()


@asynccontextmanager
async def open_session(
    params: CrawlParams,
    cache_dir: Path | str | None = None,
    browser: BrowserBackend | None = None,
    proxy_lease: ProxyLease | None = None,
    proxy_policy: str = "never",
    browser_job: BrowserJobContext | None = None,
) -> AsyncIterator[CrawlSession]:
    """Build the fetch stack for one crawl and guarantee it is torn down.

    `browser` is for a caller that outlives one crawl. The worker builds four of
    these sessions at once and would otherwise start four browsers, on top of
    the four in every other worker container — sixteen instances of a program
    whose own documentation calls it memory-hungry. A renderer passed in here is
    borrowed, not owned, so leaving the block does not close it.
    """
    cache = ResponseCache(
        cache_dir or ".",
        mode=params.cache_mode if cache_dir else "off",
        max_age=params.cache_max_age_seconds,
    )
    limiter = HostLimiter(params.delay, params.concurrency)
    owned = browser is None
    always_proxy = proxy_lease is not None and proxy_policy == "always"
    fallback_proxy = proxy_lease is not None and proxy_policy == "fallback"
    if browser is None:
        browser = BrowserRenderer(
            params.browser != "never", proxy_lease=proxy_lease if always_proxy else None
        )

    if always_proxy and not owned:
        raise ValueError("a proxied job must use its own browser identity")

    browser_context = browser.open_session(browser_job)
    browser_session = await browser_context.__aenter__()

    async with httpx.AsyncClient(
        headers={"user-agent": USER_AGENT}, timeout=REQUEST_TIMEOUT, follow_redirects=True,
        proxy=proxy_lease.url if always_proxy and proxy_lease else None,
    ) as client:
        proxy_client: httpx.AsyncClient | None = None
        proxy_browser: BrowserRenderer | None = None
        proxy_browser_context: Any = None
        proxy_browser_session: BrowserSession | None = None
        proxy_fetcher: Fetcher | None = None
        if fallback_proxy and proxy_lease:
            # A storefront's adaptive delay describes the direct network
            # identity. Carrying that cooldown into Decodo would pay for a new
            # identity and then leave it parked behind the old IP's rate limit.
            # The proxy still has its own full limiter and learns any limit the
            # shop applies to that route independently.
            proxy_limiter = HostLimiter(params.delay, params.concurrency)
            proxy_client = httpx.AsyncClient(
                headers={"user-agent": USER_AGENT}, timeout=REQUEST_TIMEOUT,
                follow_redirects=True, proxy=proxy_lease.url,
            )
            proxy_browser = BrowserRenderer(
                params.browser != "never", pages=1, proxy_lease=proxy_lease
            )
            proxy_browser_context = proxy_browser.open_session()
            proxy_browser_session = await proxy_browser_context.__aenter__()
            proxy_fetcher = Fetcher(
                proxy_client, proxy_limiter, proxy_browser_session, params.browser, cache=cache,
                impersonate_policy=params.impersonate, robots_policy=params.robots,
                stale_on_error=params.stale_on_error, proxy_lease=proxy_lease,
            )
        fetcher = Fetcher(
            client, limiter, browser_session, params.browser, cache=cache,
            impersonate_policy=params.impersonate,
            robots_policy=params.robots,
            stale_on_error=params.stale_on_error,
            proxy_lease=proxy_lease if always_proxy else None,
            proxy_fallback=proxy_fetcher,
        )
        session = CrawlSession(
            client=client, limiter=limiter, browser=browser_session, cache=cache, fetcher=fetcher
        )
        try:
            yield session
        finally:
            try:
                await browser_context.__aexit__(None, None, None)
            except Exception:
                LOGGER.warning("session.browser_session_close_failed", exc_info=True)
            if proxy_browser_context is not None:
                try:
                    await proxy_browser_context.__aexit__(None, None, None)
                except Exception:
                    LOGGER.warning("session.proxy_browser_session_close_failed", exc_info=True)
            # Not conditional on how the block was left. A cancelled run that
            # leaves camoufox running leaks a browser process per attempt, and
            # a long-lived worker would accumulate one per cancelled job.
            #
            # Conditional on ownership, though: a borrowed renderer is still
            # serving the other jobs this process is running, and closing it
            # here would take the browser out from under them.
            if owned:
                try:
                    await browser.shutdown()
                except Exception:
                    LOGGER.warning("session.browser_close_failed", exc_info=True)
            if proxy_browser is not None:
                try:
                    await proxy_browser.close()
                except Exception:
                    LOGGER.warning("session.proxy_browser_close_failed", exc_info=True)
            if proxy_client is not None:
                await proxy_client.aclose()
            # A scraper may deliberately rotate a poisoned storefront session.
            # The context variables above still name the original clients, so
            # close their replacements explicitly as well.
            if fetcher.client is not client:
                await fetcher.client.aclose()
            if (
                proxy_fetcher is not None
                and proxy_client is not None
                and proxy_fetcher.client is not proxy_client
            ):
                await proxy_fetcher.client.aclose()


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
        "stale_on_error": params.stale_on_error,
        "pipeline": params.pipeline,
        "refresh_mode": params.refresh_mode,
        "proxy_policy": params.proxy_policy,
        "proxy_max_megabytes": params.proxy_max_megabytes,
        "limit": params.limit,
    }
