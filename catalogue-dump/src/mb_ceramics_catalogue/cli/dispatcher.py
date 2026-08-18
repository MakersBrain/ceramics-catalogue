"""Publish committed catalogue jobs to the configured delivery provider."""

from __future__ import annotations

import argparse
import asyncio
import signal

from mb_ceramics_catalogue.config.settings import Settings
from mb_ceramics_catalogue.observability import logging as obs
from mb_ceramics_catalogue.observability import server
from mb_ceramics_catalogue.ops.dispatcher import Dispatcher
from mb_ceramics_catalogue.ops.providers.factory import publisher, recovery_consumer
from mb_ceramics_catalogue.storage import db


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="catalogue-dispatcher", description=__doc__)
    result.add_argument("--once", action="store_true")
    result.add_argument(
        "--reconstruct", action="store_true",
        help="republish every current eligible generation after provider state loss",
    )
    result.add_argument("--metrics-port", type=int, default=9110)
    result.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return result


async def run(options: argparse.Namespace) -> int:
    settings = Settings()
    obs.configure(options.log_level, json=settings.log_json)
    dsn = db.dsn_from_environment(settings.dsn)
    broker = publisher(settings)
    async with db.pool(dsn, minimum=1, maximum=2) as pool:
        dispatcher = Dispatcher(pool, broker, recovery_consumer(settings))
        listener = server.serve(options.metrics_port, dispatcher.describe)
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, setattr, dispatcher, "stopping", True)
        try:
            if options.reconstruct:
                await dispatcher.reconstruct()
            await dispatcher.run(once=options.once)
        finally:
            await dispatcher.close()
            if listener is not None:
                listener.shutdown()
    return 0


def main() -> int:
    return asyncio.run(run(parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
