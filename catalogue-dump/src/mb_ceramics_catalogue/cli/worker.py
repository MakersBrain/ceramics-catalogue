"""`catalogue-worker`: claim jobs from the queue and run them.

    catalogue-worker                            a plain worker
    catalogue-worker --capabilities browser     one that can take browser jobs
    catalogue-worker --once                     take one job and exit, for tests

Scaled by running more of them. `docker compose up --scale worker=3` and
`systemctl enable catalogue-worker@{1,2,3}` are the same operation; the queue
does not care how many there are, and `catalogue.hosts` is what stops three
workers tripling the load on every shop.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from mb_ceramics_catalogue import __version__
from mb_ceramics_catalogue.config.settings import Settings
from mb_ceramics_catalogue.config.sources import SourcesFile, default_path
from mb_ceramics_catalogue.observability import logging as obs
from mb_ceramics_catalogue.observability import server, tracing
from mb_ceramics_catalogue.ops.worker import Worker
from mb_ceramics_catalogue.storage import db


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="catalogue-worker", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--dsn", default="", help="libpq connection string; defaults to $CATALOGUE_DSN")
    parser.add_argument("--sources-file", type=Path, default=None)
    parser.add_argument(
        "--capabilities",
        default="",
        help="comma-separated capabilities this worker advertises, e.g. 'browser'. "
             "A job may only be claimed by a worker advertising everything it requires",
    )
    parser.add_argument("--cache", type=Path, default=None,
                        help="shared response cache directory; defaults to $CATALOGUE_CACHE_DIR")
    parser.add_argument("--dumps", type=Path, default=None,
                        help="where NDJSON artifacts are written, namespaced <run>/<job>/")
    parser.add_argument("--once", action="store_true",
                        help="take at most one job and exit")
    parser.add_argument("--metrics-port", type=int, default=9109, metavar="PORT",
                        help="serve /metrics and /health on this port (0 to disable)")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    parser.add_argument("--log-json", action="store_true")
    return parser


async def run(options: argparse.Namespace) -> int:
    settings = Settings()
    obs.configure(options.log_level, json=options.log_json or settings.log_json)
    tracing.configure("catalogue-worker")

    if options.dsn:
        settings.dsn = options.dsn
    settings.dsn = db.dsn_from_environment(settings.dsn)
    if options.cache is not None:
        settings.cache_dir = options.cache
    if options.dumps is not None:
        settings.dumps_dir = options.dumps

    capabilities = [item.strip() for item in options.capabilities.split(",") if item.strip()]
    sources = SourcesFile.load(options.sources_file or default_path())

    # Two connections at least: the heartbeat must keep beating while a job
    # holds one for the length of a crawl.
    async with db.pool(settings.dsn, minimum=2, maximum=6) as pool:
        worker = Worker(pool, sources, settings, capabilities=capabilities, once=options.once)
        # `--once` is used by tests and by a one-shot backfill; binding a port
        # for a process that exits in seconds only produces address-in-use noise
        # when several run together.
        listener = None if options.once else server.serve(options.metrics_port, worker.describe)
        worker.install_signal_handlers()
        try:
            await worker.run()
        finally:
            if listener is not None:
                listener.shutdown()

    # A worker that found nothing to do has not failed. Exit status is about
    # whether the process ran correctly, not about whether the queue was busy.
    return 0


def main() -> int:
    options = build_parser().parse_args()
    try:
        return asyncio.run(run(options))
    except KeyboardInterrupt:  # pragma: no cover - the signal handler normally wins
        return 130
    except ValueError as error:
        print(f"catalogue-worker: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
