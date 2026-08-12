"""Live progress for a run, and a log of what went wrong while it ran.

A crawl of twenty sources is twenty independent things happening at once, and
the interesting questions during one are always the same: is anything stuck, how
fast is it going, and is one source failing while the others are fine. A single
aggregate bar answers none of them, so this renders a row per source with its
own counts and rate, and keeps the most recent errors visible underneath rather
than scrolling them past.

The table reads live `ScrapeResult` objects — the same ones the scrapers append
to — so nothing has to be reported twice.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any

try:  # pragma: no cover - presence depends on the environment
    from rich.console import Console, Group
    from rich.live import Live
    from rich.logging import RichHandler
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError:  # pragma: no cover
    Live = None  # type: ignore[assignment,misc]

from mb_ceramics_catalogue.observability import logging as obs

LOGGER = logging.getLogger("catalogue.ui.dashboard")


def available(force: bool = False) -> bool:
    """rich has to be installed, and the output has to be worth redrawing."""
    return Live is not None and (force or sys.stderr.isatty())


def _clock(seconds: float) -> str:
    minutes, remainder = divmod(int(seconds), 60)
    return f"{minutes:d}:{remainder:02d}"


class Dashboard:
    """A live table of sources, with an error log below it."""

    #: Errors kept on screen; the manifest keeps the full list.
    LOG_LINES = 8

    def __init__(self, total: int, refresh: float = 4.0) -> None:
        self.total = total
        self.refresh = refresh
        self.console = Console(stderr=True)
        #: The one handler this display owns, so `release_logging` can remove
        #: exactly it rather than restoring a snapshot of the whole list.
        self.rich_handler: logging.Handler | None = None
        self.quiet: Any = None
        self.started_at = time.monotonic()
        self.states: dict[str, dict[str, Any]] = {}
        self.results: dict[str, Any] = {}
        self.seen_errors: dict[str, int] = {}
        self.log: list[tuple[float, str, str]] = []
        self.live: Any = None
        self.task: asyncio.Task[None] | None = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self.capture_logging()
        self.live = Live(
            self.render(), refresh_per_second=self.refresh, transient=False, console=self.console,
        )
        self.live.start()
        self.task = asyncio.create_task(self._loop())

    def capture_logging(self) -> None:
        """Route logging through the same console the table is drawn on.

        A live display owns the terminal: anything else writing to the same
        stream lands in the middle of a redraw, which is why an unrouted run
        looks like a stack of half-drawn tables that never progresses. rich
        prints log lines above the live region instead, so both stay readable.

        This used to do `root.handlers = [RichHandler(...)]`, which is the
        problem §4.6 of the plan is about: constructing a display silently
        removed every other sink, including the structured JSON a worker writes
        to stdout — the one thing you need to debug it. So the rich handler is
        *added*, and the plain console handler is quietened through
        `observability.logging.quieten()` rather than being replaced. Any other
        sink — the database, a log shipper — keeps receiving everything.
        """
        self.rich_handler = obs.attach(
            RichHandler(
                console=self.console,
                show_path=False,
                rich_tracebacks=False,
                log_time_format="%H:%M:%S",
            )
        )
        self.quiet = obs.quieten()
        self.quiet.__enter__()

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(1 / self.refresh)
                self.collect()
                if self.live:
                    self.live.update(self.render())
        except asyncio.CancelledError:
            pass
        except Exception:
            LOGGER.debug("live display stopped after an error", exc_info=True)

    def stop(self) -> None:
        """Take the display down, and never let doing so break the run.

        By the time this runs the records are collected and only the writing is
        left; a rendering failure here would throw away a whole crawl.
        """
        if self.task:
            self.task.cancel()
        try:
            if self.live:
                self.collect()
                self.live.update(self.render())
                self.live.stop()
        except Exception:
            LOGGER.debug("could not close the live display cleanly", exc_info=True)
        finally:
            self.live = None
            self.release_logging()

    def release_logging(self) -> None:
        """Remove exactly the handler this display added, and nothing else."""
        if self.rich_handler is not None:
            obs.detach(self.rich_handler)
            self.rich_handler = None
        if self.quiet is not None:
            self.quiet.__exit__(None, None, None)
            self.quiet = None

    # -- state ------------------------------------------------------------

    def track(self, source: str, result: Any, scraper: str = "", method: str = "") -> None:
        self.results[source] = result
        state = self.states.setdefault(source, {"status": "running", "started": time.monotonic()})
        if scraper:
            state["scraper"] = scraper
        if method:
            state["method"] = method

    def finish(self, source: str, summary: dict[str, Any]) -> None:
        state = self.states.setdefault(source, {"status": "running", "started": time.monotonic()})
        state["status"] = "failed" if summary.get("error_count") and not summary["records"] else "done"
        state["records"] = summary["records"]
        state["requests"] = summary["requests"]
        state["finished"] = time.monotonic()

    def collect(self) -> None:
        """Pull new errors out of the live results into the log pane."""
        for source, result in self.results.items():
            errors = getattr(result, "errors", [])
            already = self.seen_errors.get(source, 0)
            for entry in errors[already:]:
                message = str(entry.get("error", "")).splitlines()[0][:110]
                self.log.append((time.monotonic(), source, message))
            self.seen_errors[source] = len(errors)
        del self.log[: max(0, len(self.log) - self.LOG_LINES * 4)]

    def counts(self, source: str) -> tuple[int, int, int]:
        result = self.results.get(source)
        if result is None:
            state = self.states.get(source, {})
            return state.get("records", 0), state.get("requests", 0), 0
        return (
            len(getattr(result, "records", [])),
            getattr(result, "requests", 0) + getattr(result, "rendered_pages", 0),
            len(getattr(result, "errors", [])),
        )

    # -- rendering --------------------------------------------------------

    def render(self) -> Any:
        elapsed = time.monotonic() - self.started_at
        table = Table(expand=True, pad_edge=False, box=None, header_style="bold")
        table.add_column("source", overflow="ellipsis", no_wrap=True)
        # Which reader is running and how it is reading, so a source quietly
        # falling back to the browser is visible while it happens rather than
        # only in the manifest afterwards.
        table.add_column("scraper", overflow="ellipsis", no_wrap=True, style="dim")
        table.add_column("method", overflow="ellipsis", no_wrap=True, style="dim")
        table.add_column("status", width=9)
        table.add_column("records", justify="right", width=8)
        table.add_column("requests", justify="right", width=9)
        table.add_column("req/s", justify="right", width=6)
        table.add_column("errors", justify="right", width=7)

        done = records_total = requests_total = errors_total = 0
        for source in sorted(self.states, key=lambda name: (self.states[name]["status"] != "running", name)):
            state = self.states[source]
            records, requests, errors = self.counts(source)
            span = (state.get("finished") or time.monotonic()) - state["started"]
            rate = requests / span if span > 0.5 else 0.0
            status = state["status"]
            colour = {"running": "cyan", "done": "green", "failed": "red"}.get(status, "white")
            rendered = getattr(self.results.get(source), "rendered_pages", 0)
            method = state.get("method", "")
            if rendered:
                method = f"{method}+browser" if method != "browser" else method
            table.add_row(
                source,
                state.get("scraper", ""),
                method,
                Text(status, style=colour),
                f"{records:,}",
                f"{requests:,}",
                f"{rate:.1f}",
                Text(f"{errors:,}", style="red" if errors else "dim"),
            )
            done += status != "running"
            records_total += records
            requests_total += requests
            errors_total += errors

        overall = requests_total / elapsed if elapsed > 0.5 else 0.0
        header = Text.assemble(
            ("catalogue-dump  ", "bold"),
            (f"{done}/{self.total} sources", "bold cyan"),
            ("   ", ""),
            (f"{records_total:,} records", "bold"),
            ("   ", ""),
            (f"{overall:.1f} req/s", ""),
            ("   ", ""),
            (f"elapsed {_clock(elapsed)}", "dim"),
            ("   ", ""),
            (f"{errors_total:,} errors", "red" if errors_total else "dim"),
        )

        panels = [Panel(table, title=header, title_align="left", border_style="dim")]
        if self.log:
            recent = self.log[-self.LOG_LINES:]
            lines = Text()
            for index, (when, source, message) in enumerate(recent):
                if index:
                    lines.append("\n")
                lines.append(f"{_clock(when - self.started_at):>6}  ", style="dim")
                lines.append(f"{source:<22}", style="yellow")
                lines.append(message, style="red")
            panels.append(Panel(lines, title="recent errors", title_align="left", border_style="red"))
        return Group(*panels)
