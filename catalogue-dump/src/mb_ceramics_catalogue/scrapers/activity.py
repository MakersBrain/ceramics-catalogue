"""What each source is doing right now.

The scrapers all share one fetcher, so a request carries no obvious mark of
which source asked for it. A context variable does carry it: every source runs
in its own asyncio task, and a task inherits the context it was created in, so
anything fetched inside a source's task can be attributed to that source without
threading a name through every call.

This module holds no rendering and imports nothing beyond the standard library.
The display reads it; the crawl writes to it.
"""

from __future__ import annotations

import time
from collections import deque
from contextvars import ContextVar
from typing import Any

#: Set once per source, in the task that runs it.
CURRENT_SOURCE: ContextVar[str] = ContextVar("catalogue_source", default="")

#: Set once per job, in the task the worker runs it in. The same reasoning as
#: `CURRENT_SOURCE` applied to the other identifier a line can belong to: a
#: worker running four jobs at once has one process-wide root logger, so a log
#: sink can only tell whose line it is holding by asking the task it arrived in.
#: Empty outside a job — the heartbeat and the queue belong to no job's log.
CURRENT_JOB: ContextVar[str] = ContextVar("catalogue_job", default="")

#: Requests kept per source: enough to see what a source is working through.
HISTORY = 40


class Activity:
    """In-flight and recently finished requests, per source."""

    def __init__(self) -> None:
        self.live: dict[str, dict[str, float]] = {}
        self.recent: dict[str, deque[tuple[float, str, str]]] = {}
        self.counts: dict[str, int] = {}

    def started(self, url: str) -> None:
        source = CURRENT_SOURCE.get()
        if not source:
            return
        self.live.setdefault(source, {})[url] = time.monotonic()

    def finished(self, url: str, outcome: str = "ok") -> None:
        source = CURRENT_SOURCE.get()
        if not source:
            return
        began = self.live.get(source, {}).pop(url, None)
        history = self.recent.setdefault(source, deque(maxlen=HISTORY))
        history.appendleft((time.monotonic() - began if began else 0.0, outcome, url))
        self.counts[source] = self.counts.get(source, 0) + 1

    def in_flight(self, source: str) -> list[tuple[float, str]]:
        now = time.monotonic()
        return sorted(
            ((now - began, url) for url, began in self.live.get(source, {}).items()),
            reverse=True,
        )

    def history(self, source: str) -> list[tuple[float, str, str]]:
        return list(self.recent.get(source, ()))


#: One per process; the fetcher writes to it and the display reads it.
ACTIVITY = Activity()


def describe(value: Any, width: int = 96) -> str:
    text = str(value)
    return text if len(text) <= width else text[: width - 1] + "…"
