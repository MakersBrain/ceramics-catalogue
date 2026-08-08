"""Run parameters and process settings, as two models rather than a Namespace.

The split matters. `CrawlParams` is *what to do* — the options a run carries,
which arrive from an `argparse.Namespace` on the command line and from a job's
`params` jsonb in the database, and must mean the same thing either way.
`Settings` is *where the process lives* — the DSN, the cache directory, the
control token. One comes from an operator's request, the other from the
deployment, and conflating them is how a run ends up able to change the database
it writes to.

`CrawlParams` is also what generates the request schema for `POST /v1/runs`
(§6), so the CLI, the API and the scheduler cannot disagree about what a valid
run is.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BrowserPolicy = Literal["never", "auto", "always"]
CacheMode = Literal["off", "auto", "replay", "refresh"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

#: Seconds one source may run before the crawl gives up on it. Generous, because
#: a large storefront legitimately takes an hour; finite, because before this
#: there was no deadline at all and a hung source held its slot until someone
#: noticed — which, on a 03:00 schedule, is the morning.
DEFAULT_SOURCE_TIMEOUT = 3600.0


class CrawlParams(BaseModel):
    """Everything that decides how a run collects. Validated once, used twice."""

    model_config = ConfigDict(extra="forbid")

    #: Maximum products per source. A run that hits this is `truncated`, and a
    #: truncated run is never grounds for retiring a product (see `plan_load`).
    limit: int | None = Field(default=None, ge=1)
    #: Sources crawled at once.
    sources: int = Field(default=4, ge=1)
    #: Most requests in flight per host.
    concurrency: int = Field(default=8, ge=1)
    #: Seconds one crawling slot waits between its requests to a host.
    delay: float = Field(default=0.0, ge=0)
    browser: BrowserPolicy = "auto"

    cache_mode: CacheMode = "auto"
    #: How old a stored response may be before `auto` refetches it. Zero means
    #: never stale.
    #:
    #: The default is deliberately **20 hours, not the 168 it used to be**. A
    #: daily price run under a seven-day max age replays yesterday's pages and
    #: reports success while changing no prices at all, which would make the
    #: whole schedule a no-op. Seven days is right for reworking a parser and
    #: wrong for the thing this pipeline exists to do (§8).
    cache_max_age_hours: float = Field(default=20.0, ge=0)

    #: Per-source deadline; a source may lower it but not raise it.
    source_timeout_seconds: float = Field(default=DEFAULT_SOURCE_TIMEOUT, gt=0)

    log_level: LogLevel = "INFO"
    dry_run: bool = False
    #: Let an empty result replace an existing dump. Off, because an empty
    #: scrape is not a smaller catalogue.
    allow_empty: bool = False

    @property
    def cache_max_age_seconds(self) -> float | None:
        return (self.cache_max_age_hours * 3600) or None

    @model_validator(mode="after")
    def _replay_needs_no_browser(self) -> CrawlParams:
        """Replay must not be able to reach the network by another door.

        `--cache-mode replay` promises no requests. A browser render is a
        request that does not go through the response cache's HTTP path, so
        leaving the browser enabled would quietly turn an offline parser run
        into a live crawl of the two sources that need one.
        """
        if self.cache_mode == "replay" and self.browser == "always":
            raise ValueError("cache_mode=replay cannot be combined with browser=always")
        return self

    def timeout_for(self, source_timeout: float | None) -> float:
        """The deadline for one source: the stricter of its own and the run's."""
        if source_timeout is None:
            return self.source_timeout_seconds
        return min(source_timeout, self.source_timeout_seconds)

    @classmethod
    def from_namespace(cls, options: argparse.Namespace) -> CrawlParams:
        """Build from parsed command-line arguments.

        Named explicitly rather than by `vars(options)`: the parser also carries
        things that are not run parameters (where to write, which sources), and
        a model with `extra="forbid"` would reject them — correctly.
        """
        return cls(
            limit=options.limit,
            sources=options.sources,
            concurrency=options.concurrency,
            delay=options.delay,
            browser=options.browser,
            cache_mode=options.cache_mode if options.cache else "off",
            cache_max_age_hours=options.cache_max_age,
            source_timeout_seconds=options.source_timeout,
            log_level=options.log_level,
            dry_run=options.dry_run,
            allow_empty=options.allow_empty,
        )

    @classmethod
    def from_job(cls, params: dict[str, Any] | None) -> CrawlParams:
        """Build from a job's `params` jsonb, taking defaults for what it omits."""
        return cls.model_validate(params or {})


class Settings(BaseSettings):
    """Where this process runs, from the environment.

    Read once at startup. Nothing here is settable per run, which is the point:
    a run request arriving over HTTP cannot redirect the load to another
    database or move the artifact directory.
    """

    model_config = SettingsConfigDict(
        env_prefix="CATALOGUE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    #: libpq connection string for the catalogue database.
    dsn: str = ""
    #: Where recorded responses live. Shared between workers as a named volume,
    #: sharded per host so two of them never write one entry (§8).
    cache_dir: Path = Path(".cache")
    #: Where NDJSON artifacts are written, namespaced `<run-id>/<job-id>/`.
    dumps_dir: Path = Path("dumps")
    #: Identifies this worker's build in `catalogue.workers.version`.
    worker_capabilities: tuple[str, ...] = ()
    #: Exit cleanly after this many completed jobs, letting the restart policy
    #: start a fresh process. Zero means never.
    #:
    #: This exists for the browser worker. camoufox leaks across jobs, and a
    #: long-lived process that has rendered a few hundred pages is a process
    #: that will eventually be killed by the OOM reaper mid-write. Recycling on
    #: a count turns that into a scheduled, graceful restart between jobs.
    max_jobs: int = 0
    #: Bearer token `catalogue-control` requires on every /v1 route.
    control_token: str = ""

    #: Emit logs as one JSON object per line regardless of whether stdout is a
    #: terminal. Set in containers, where the console renderer is unreadable.
    log_json: bool | None = None

    def sources_path(self) -> Path:
        from .sources import default_path

        return default_path()
