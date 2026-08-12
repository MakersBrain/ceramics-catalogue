"""Where progress goes, separated from what progress is.

`ProgressReporter` in `dump.py` did three jobs: it decided which display to use,
it held state, and it rendered. The third is what the worker needs to replace —
it reports to a database, not a terminal — so it comes out as a sink and the
other two stay put.

Sinks are **additive**. A worker run with a TTY attached can have both a
terminal view and a Postgres one, and that is only true because
`observability.logging` stopped the display from swapping the root logger's
handler list out from under everything else (§4.6).

Because every sink reads the live `ScrapeResult` objects the scrapers already
append to, no scraper changes and nothing is reported twice. A ticker samples at
1 Hz rather than the scrapers pushing.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import Iterable
from contextlib import suppress
from typing import Any, Protocol, runtime_checkable

from mb_ceramics_catalogue.observability import logging as obs

LOGGER = obs.get_logger("catalogue.progress")

#: How often the ticker samples every running source. One second is what the eye
#: reads as live, and it is also the rate the Postgres sink throttles to, so a
#: source doing three thousand requests does three thousand *fetches* and about
#: as many progress writes as it ran seconds.
SAMPLE_SECONDS = 1.0


@runtime_checkable
class ProgressSink(Protocol):
    """One destination for what a run is doing.

    Every method must tolerate being called for a source it has never seen, and
    none may raise: a sink that fails takes out the display, never the crawl.
    """

    async def started(self, source: str, result: Any, scraper: str = "", method: str = "") -> None: ...

    async def progress(self, source: str, result: Any) -> None: ...

    async def finished(self, source: str, summary: dict[str, Any]) -> None: ...

    async def close(self) -> None: ...


class Progress:
    """The state of a run, and the sinks it is reported to.

    Holds the live `ScrapeResult` per source — which is what makes
    `partial_result` possible after a cancellation — and drives a ticker that
    samples them, so the scrapers never call a reporting method.
    """

    def __init__(self, total: int, sinks: Iterable[ProgressSink] = ()) -> None:
        self.total = total
        self.sinks: list[ProgressSink] = list(sinks)
        #: The live ScrapeResult per source, so counts need no second report.
        self.results: dict[str, Any] = {}
        self.running: set[str] = set()
        self._ticker: asyncio.Task[None] | None = None

    def add(self, sink: ProgressSink) -> None:
        self.sinks.append(sink)

    async def _each(self, method: str, *args: Any) -> None:
        for sink in self.sinks:
            try:
                await getattr(sink, method)(*args)
            except Exception:
                LOGGER.debug("progress.sink_failed", sink=type(sink).__name__, method=method, exc_info=True)

    async def started(self, source: str, result: Any, scraper: str = "", method: str = "") -> None:
        self.results[source] = result
        self.running.add(source)
        await self._each("started", source, result, scraper, method)

    async def finished(self, source: str, summary: dict[str, Any]) -> None:
        self.running.discard(source)
        await self._each("finished", source, summary)

    async def sample(self) -> None:
        """Push the current counters for every running source, once."""
        for source in sorted(self.running):
            result = self.results.get(source)
            if result is not None:
                await self._each("progress", source, result)

    async def __aenter__(self) -> Progress:
        self._ticker = asyncio.create_task(self._tick(), name="progress-ticker")
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._ticker is not None:
            self._ticker.cancel()
            with suppress(asyncio.CancelledError):
                await self._ticker
            self._ticker = None
        # One last sample so the final counts land before anything is torn down;
        # otherwise a fast source can finish between ticks and never be seen at
        # its true size.
        await self.sample()
        for sink in self.sinks:
            try:
                await sink.close()
            except Exception:
                LOGGER.debug("progress.close_failed", sink=type(sink).__name__, exc_info=True)

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(SAMPLE_SECONDS)
            await self.sample()


class LogSink:
    """One structured line per event. What a redirected run gets.

    A redrawing table in a log file is unreadable, and a container has no
    terminal at all, so this is the default rather than the fallback.
    """

    async def started(self, source: str, result: Any, scraper: str = "", method: str = "") -> None:
        LOGGER.info("job.started", source=source, scraper=scraper, method=method)

    async def progress(self, source: str, result: Any) -> None:
        # Deliberately debug: at 1 Hz across eighty sources this is eighty lines
        # a second, which is a level, not an event (§3.1).
        LOGGER.debug(
            "job.progress",
            source=source,
            records=len(result.records),
            requests=result.requests,
            errors=len(result.errors),
        )

    async def finished(self, source: str, summary: dict[str, Any]) -> None:
        LOGGER.info(
            "job.finished",
            source=source,
            records=summary["records"],
            requests=summary["requests"],
            rendered=summary["rendered_pages"],
            errors=summary["error_count"],
            truncated=summary["truncated"],
        )

    async def close(self) -> None:
        return None


class BarSink:
    """The single redrawing line on stderr, for a terminal without Textual."""

    WIDTH = 26

    def __init__(self, total: int) -> None:
        self.total = total
        self.lock = asyncio.Lock()
        self.states: dict[str, dict[str, Any]] = {}

    async def started(self, source: str, result: Any, scraper: str = "", method: str = "") -> None:
        self.states[source] = {"status": "running", "records": 0}
        await self._render()

    async def progress(self, source: str, result: Any) -> None:
        state = self.states.setdefault(source, {"status": "running"})
        state["records"] = len(result.records)
        await self._render()

    async def finished(self, source: str, summary: dict[str, Any]) -> None:
        self.states[source] = {"status": "done", "records": summary["records"]}
        await self._render()

    async def _render(self) -> None:
        async with self.lock:
            done = sum(state["status"] == "done" for state in self.states.values())
            records = sum(state.get("records", 0) for state in self.states.values())
            active = [name for name, state in self.states.items() if state["status"] == "running"]
            filled = round(self.WIDTH * done / self.total) if self.total else self.WIDTH
            bar = (
                "=" * max(0, filled - 1)
                + (">" if done < self.total else "=")
                + " " * max(0, self.WIDTH - filled)
            )
            line = f"\r\033[2Kcatalogue [{bar}] {done}/{self.total} sources | {records} records"
            if active:
                line += " | " + ", ".join(active[:3]) + (
                    f" +{len(active) - 3}" if len(active) > 3 else ""
                )
            sys.stderr.write(line)
            sys.stderr.flush()

    async def close(self) -> None:
        sys.stderr.write("\n")
        sys.stderr.flush()


class DashboardSink:
    """The rich redrawing table from `ui.dashboard`."""

    def __init__(self, total: int) -> None:
        from mb_ceramics_catalogue.ui import dashboard

        self.dashboard = dashboard.Dashboard(total)
        self.dashboard.start()

    async def started(self, source: str, result: Any, scraper: str = "", method: str = "") -> None:
        self.dashboard.track(source, result, scraper, method)

    async def progress(self, source: str, result: Any) -> None:
        return None  # the dashboard reads the live results on its own schedule

    async def finished(self, source: str, summary: dict[str, Any]) -> None:
        self.dashboard.finish(source, summary)

    async def close(self) -> None:
        self.dashboard.stop()


class InteractiveSink:
    """The Textual app from `ui.interactive`, running beside the crawl."""

    def __init__(self, total: int, on_stop: Any = None) -> None:
        from mb_ceramics_catalogue.ui import interactive

        self.module = interactive
        self.state = interactive.RunState(total)
        self.on_stop = on_stop
        self.app: Any = None
        self.task: asyncio.Task[None] | None = None
        self.relay: logging.Handler | None = None
        self._quiet: Any = None

    async def open(self) -> None:
        """Start the app and wait until it has actually taken the terminal.

        The old code slept 200 ms here with a comment saying Textual needed a
        moment. A sleep standing in for a synchronisation primitive is a race
        that passes on a fast machine; `Textual`'s own mount signal is the
        thing that was actually being waited for.
        """
        self.relay = obs.attach(self.module.LogRelay(self.state))
        self._quiet = obs.quieten()
        self._quiet.__enter__()
        self.app = self.module.CrawlApp(self.state, on_stop=self.on_stop)
        self.task = asyncio.create_task(self.app.run_async(), name="interactive-app")
        ready = getattr(self.app, "ready", None)
        if ready is not None:
            try:
                async with asyncio.timeout(5):
                    await ready.wait()
            except TimeoutError:  # pragma: no cover - a slow terminal, not a fault
                LOGGER.debug("interactive.slow_mount")

    async def started(self, source: str, result: Any, scraper: str = "", method: str = "") -> None:
        self.state.track(source, result, scraper, method)

    async def progress(self, source: str, result: Any) -> None:
        return None  # the app polls the live results itself, at its own rate

    async def finished(self, source: str, summary: dict[str, Any]) -> None:
        self.state.finish(source, summary)

    async def close(self) -> None:
        if self.app is not None:
            # Let the last counts land before the screen goes away.
            await asyncio.sleep(0.4)
            self.app.exit()
            if self.task is not None:
                await asyncio.wait([self.task], timeout=5)
            self.app = None
        if self.relay is not None:
            obs.detach(self.relay)
            self.relay = None
        if self._quiet is not None:
            self._quiet.__exit__(None, None, None)
            self._quiet = None


def terminal_sinks(
    total: int,
    *,
    force: bool = False,
    disabled: bool = False,
    plain: bool = False,
    on_stop: Any = None,
) -> list[ProgressSink]:
    """Pick the display for what the output can actually do.

    Selection used to live in `ProgressReporter.__init__`, which meant the
    reporter both chose the display and was the display. Here it is one function
    returning sinks, so a worker composes its own list and never runs this at
    all.
    """
    if disabled or not (force or sys.stderr.isatty()):
        return [LogSink()]

    if not plain:
        try:
            from mb_ceramics_catalogue.ui import interactive  # noqa: F401

            if sys.stdout.isatty():
                return [InteractiveSink(total, on_stop=on_stop)]
        except ImportError:  # pragma: no cover - depends on the environment
            pass

    try:
        from mb_ceramics_catalogue.ui import dashboard

        if dashboard.available(force):
            return [DashboardSink(total)]
    except ImportError:  # pragma: no cover
        pass

    return [BarSink(total)]


class Timer:
    """Wall time for one source, for `catalogue_job_duration_seconds`."""

    def __init__(self) -> None:
        self.started = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self.started
