"""An interactive view of a run: scroll the sources, open one, read its log.

The non-interactive dashboard in `tui.py` answers "is it moving"; this answers
"what is that source actually doing". Sources scroll, the log scrolls, and
Enter opens one source to show the requests it has in flight, the ones it just
finished, and its own log lines — the questions that otherwise mean tailing a
log file and grepping for a source name while the run is still going.

The crawl owns the event loop; this is a Textual app running beside it, reading
the same live `ScrapeResult` objects and the shared activity record. It never
writes to them, so removing the display cannot change what is collected.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Label, RichLog, Static

from ateliera_catalogue.scrapers.activity import ACTIVITY, CURRENT_SOURCE, describe

LOGGER = logging.getLogger("catalogue.ui.interactive")


def clock(seconds: float) -> str:
    minutes, remainder = divmod(int(max(0.0, seconds)), 60)
    return f"{minutes:d}:{remainder:02d}"


class LogRelay(logging.Handler):
    """Keeps log records for the whole run and per source, for the panes."""

    def __init__(self, state: RunState) -> None:
        super().__init__()
        self.state = state

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad log line must not stop a crawl
            return
        entry = (time.monotonic(), record.levelno, record.name, message)
        self.state.log.append(entry)
        # Attributed by the context the log call was made in, so a source's own
        # lines follow it into its detail view.
        if source := getattr(record, "source", "") or CURRENT_SOURCE.get():
            self.state.source_log.setdefault(source, deque(maxlen=200)).append(entry)


class RunState:
    """Everything the display reads, written by the crawl as it goes."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.started_at = time.monotonic()
        self.sources: dict[str, dict[str, Any]] = {}
        self.results: dict[str, Any] = {}
        self.log: deque[tuple[float, int, str, str]] = deque(maxlen=2000)
        self.source_log: dict[str, deque[tuple[float, int, str, str]]] = {}

    def track(self, source: str, result: Any, scraper: str = "", method: str = "") -> None:
        self.results[source] = result
        state = self.sources.setdefault(source, {"status": "running", "started": time.monotonic()})
        state.update({"scraper": scraper or state.get("scraper", ""),
                      "method": method or state.get("method", "")})

    def finish(self, source: str, summary: dict[str, Any]) -> None:
        state = self.sources.setdefault(source, {"status": "running", "started": time.monotonic()})
        state["status"] = "failed" if summary.get("error_count") and not summary["records"] else "done"
        state["finished"] = time.monotonic()
        state["records"] = summary.get("records", 0)
        state["requests"] = summary.get("requests", 0)

    def counts(self, source: str) -> tuple[int, int, int]:
        result = self.results.get(source)
        if result is None:
            state = self.sources.get(source, {})
            return state.get("records", 0), state.get("requests", 0), 0
        return (
            len(getattr(result, "records", [])),
            getattr(result, "requests", 0) + getattr(result, "rendered_pages", 0),
            len(getattr(result, "errors", [])),
        )

    def rate(self, source: str) -> float:
        state = self.sources.get(source, {})
        span = (state.get("finished") or time.monotonic()) - state.get("started", self.started_at)
        _, requests, _ = self.counts(source)
        return requests / span if span > 0.5 else 0.0

    def totals(self) -> dict[str, float]:
        done = records = requests = errors = 0
        for source, state in self.sources.items():
            source_records, source_requests, source_errors = self.counts(source)
            done += state["status"] != "running"
            records += source_records
            requests += source_requests
            errors += source_errors
        elapsed = time.monotonic() - self.started_at
        return {
            "done": done, "records": records, "requests": requests, "errors": errors,
            "elapsed": elapsed, "rate": requests / elapsed if elapsed > 0.5 else 0.0,
        }


class SourceDetail(ModalScreen[None]):
    """One source: what it is fetching now, what it just fetched, and its log."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,q,enter", "dismiss", "back"),
    ]

    def __init__(self, state: RunState, source: str) -> None:
        super().__init__()
        self.state = state
        self.source = source

    def compose(self) -> ComposeResult:
        with Vertical(id="detail"):
            yield Label(self.source, id="detail-title")
            yield Static(id="detail-summary")
            yield Label("in flight", classes="section")
            yield Static(id="detail-live")
            yield Label("recently fetched", classes="section")
            yield Static(id="detail-recent")
            yield Label("log", classes="section")
            yield RichLog(id="detail-log", markup=True, wrap=False, max_lines=200)

    def on_mount(self) -> None:
        self.seen = 0
        self.refresh_detail()
        self.set_interval(0.25, self.refresh_detail)

    def refresh_detail(self) -> None:
        state = self.state.sources.get(self.source, {})
        records, requests, errors = self.state.counts(self.source)
        self.query_one("#detail-summary", Static).update(
            f"[b]{state.get('scraper', '')}[/b] / {state.get('method', '')}   "
            f"{state.get('status', '')}   {records:,} records   {requests:,} requests   "
            f"{self.state.rate(self.source):.1f} req/s   "
            f"[{'red' if errors else 'dim'}]{errors:,} errors[/]"
        )

        live = ACTIVITY.in_flight(self.source)
        self.query_one("#detail-live", Static).update(
            "\n".join(f"[cyan]{age:5.1f}s[/]  {describe(url, 110)}" for age, url in live[:8])
            or "[dim]nothing in flight[/]"
        )

        recent = ACTIVITY.history(self.source)
        self.query_one("#detail-recent", Static).update(
            "\n".join(
                f"[dim]{took:5.2f}s[/]  [{'green' if outcome in ('200', 'cached', 'rendered') else 'red'}]"
                f"{outcome:>12}[/]  {describe(url, 100)}"
                for took, outcome, url in recent[:10]
            )
            or "[dim]nothing fetched yet[/]"
        )

        entries = list(self.state.source_log.get(self.source, ()))
        if len(entries) > self.seen:
            log = self.query_one("#detail-log", RichLog)
            for when, level, _, message in entries[self.seen:]:
                colour = "red" if level >= logging.WARNING else "dim"
                log.write(f"[dim]{clock(when - self.state.started_at):>6}[/]  [{colour}]{message}[/]")
            self.seen = len(entries)


class CrawlApp(App[None]):
    """The run, live: a scrollable source table over a scrollable log."""

    CSS = """
    Screen { layout: vertical; }
    #summary { height: 1; padding: 0 1; background: $panel; color: $text; }
    #sources { height: 3fr; border: round $primary 30%; }
    #log { height: 1fr; border: round $error 30%; padding: 0 1; }
    #detail { padding: 1 2; background: $surface; border: round $primary; }
    #detail-title { text-style: bold; }
    .section { color: $text-muted; margin-top: 1; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        # Enter is left to the table itself, which turns it into RowSelected;
        # an app-level binding for it never fires while the table has focus.
        Binding("tab", "focus_next", "move focus"),
        Binding("l", "focus_log", "log pane"),
        Binding("s", "focus_sources", "source list"),
        Binding("f", "follow", "follow newest"),
        Binding("q,ctrl+c", "stop", "stop the run"),
    ]

    COLUMNS = ("source", "scraper", "method", "status", "records", "requests", "req/s", "errors")

    def __init__(self, state: RunState, on_stop: Any = None) -> None:
        super().__init__()
        self.state = state
        self.on_stop = on_stop
        self.rows: dict[str, Any] = {}
        self.seen_log = 0
        self.follow = True
        #: Set once the app has actually taken the terminal.
        #:
        #: The crawl used to `await asyncio.sleep(0.2)` after starting this,
        #: with a comment saying Textual needed a moment. A sleep standing in
        #: for a synchronisation primitive is a race that passes on a fast
        #: machine and drops the first counts on a loaded one; this is the
        #: signal that sleep was approximating.
        self.ready = asyncio.Event()

    def compose(self) -> ComposeResult:
        yield Static(id="summary")
        with Horizontal():
            yield DataTable(id="sources", cursor_type="row", zebra_stripes=True)
        # Focusable so the log can be scrolled back through while the run goes
        # on; `f` stops it jumping to the newest line meanwhile.
        yield RichLog(id="log", markup=True, wrap=False, max_lines=1000, auto_scroll=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#sources", DataTable)
        for column in self.COLUMNS:
            table.add_column(column, key=column)
        table.focus()
        self.query_one("#log", RichLog).can_focus = True
        self.set_interval(0.25, self.refresh_state)
        self.ready.set()

    # -- updating ---------------------------------------------------------

    def refresh_state(self) -> None:
        totals = self.state.totals()
        self.query_one("#summary", Static).update(
            f"[b]catalogue-dump[/]  [cyan]{totals['done']:.0f}/{self.state.total} sources[/]   "
            f"[b]{totals['records']:,.0f} records[/]   {totals['rate']:.1f} req/s   "
            f"[dim]elapsed {clock(totals['elapsed'])}[/]   "
            f"[{'red' if totals['errors'] else 'dim'}]{totals['errors']:,.0f} errors[/]"
        )
        self.refresh_table()
        self.refresh_log()

    def refresh_table(self) -> None:
        table = self.query_one("#sources", DataTable)
        for source in sorted(self.state.sources):
            state = self.state.sources[source]
            records, requests, errors = self.state.counts(source)
            method = state.get("method", "")
            if getattr(self.state.results.get(source), "rendered_pages", 0) and method != "browser":
                method = f"{method}+browser"
            status = state["status"]
            colour = {"running": "cyan", "done": "green", "failed": "red"}.get(status, "white")
            cells = (
                source, state.get("scraper", ""), method,
                f"[{colour}]{status}[/]", f"{records:,}", f"{requests:,}",
                f"{self.state.rate(source):.1f}",
                f"[red]{errors:,}[/]" if errors else "0",
            )
            if source in self.rows:
                for column, value in zip(self.COLUMNS, cells, strict=True):
                    table.update_cell(self.rows[source], column, value)
            else:
                self.rows[source] = table.add_row(*cells, key=source)

    def refresh_log(self) -> None:
        entries = list(self.state.log)
        if len(entries) <= self.seen_log:
            return
        log = self.query_one("#log", RichLog)
        log.auto_scroll = self.follow
        for when, level, _name, message in entries[self.seen_log:]:
            colour = "red" if level >= logging.ERROR else "yellow" if level >= logging.WARNING else "dim"
            log.write(
                f"[dim]{clock(when - self.state.started_at):>6}[/]  [{colour}]{message}[/]"
            )
        self.seen_log = len(entries)

    # -- actions ----------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row: open that source."""
        if event.row_key.value:
            self.push_screen(SourceDetail(self.state, str(event.row_key.value)))

    def action_focus_log(self) -> None:
        self.query_one("#log", RichLog).focus()

    def action_focus_sources(self) -> None:
        self.query_one("#sources", DataTable).focus()

    def action_follow(self) -> None:
        """Stop the log pane jumping to the bottom, so it can be read."""
        self.follow = not self.follow
        self.notify(f"log {'follows' if self.follow else 'is held; scroll freely'}")

    def action_stop(self) -> None:
        if self.on_stop:
            self.on_stop()
        self.notify("stopping: finishing what is in flight, then writing what was collected")
