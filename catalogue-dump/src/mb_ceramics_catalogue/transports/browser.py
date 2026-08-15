"""Backend-neutral browser lifecycle contracts.

Backends live for the worker process.  Sessions live for exactly one crawl and
own every page/storage handle they create.  Keeping those lifetimes distinct is
the isolation boundary that a shared browser process needs.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

BrowserBackendName = Literal["camoufox", "cdp_extension_proxy"]


class Blocked(Exception):
    """A remote site's rules, response, or defences stopped a transport operation."""


# Neutral descriptive alias for new transport code, while preserving the
# long-standing public class name exposed by ``scrapers.base.Blocked``.
TransportBlocked = Blocked


class BrowserUnavailable(Exception):
    """The process cannot provide the browser session required by this job.

    This is deliberately distinct from :class:`TransportBlocked`: a refusal is
    local to one request, while an unavailable backend is a worker-placement
    failure that must escape page-level handlers and be requeued elsewhere.
    """


@dataclass(frozen=True, slots=True)
class BrowserJobContext:
    job_id: str
    logical_profile: str | None = None


@runtime_checkable
class BrowserSession(Protocol):
    async def render(
        self, url: str, wait_ms: int = 1500, wait_for: str | None = None,
    ) -> str: ...

    async def evaluate(
        self, url: str, script: str, wait_ms: int = 2000,
        wait_for: str | None = None,
    ) -> Any: ...

    async def request_json(
        self, page_url: str, endpoint: str, *, method: str = "POST",
        headers: dict[str, str] | None = None, body: Any = None,
    ) -> Any: ...

    async def close(self) -> None: ...


@runtime_checkable
class BrowserBackend(Protocol):
    @property
    def backend(self) -> BrowserBackendName: ...

    def open_session(
        self, job: BrowserJobContext | None = None,
    ) -> AbstractAsyncContextManager[BrowserSession]: ...

    async def shutdown(self) -> None: ...
