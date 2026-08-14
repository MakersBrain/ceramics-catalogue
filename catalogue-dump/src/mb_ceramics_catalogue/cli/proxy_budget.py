"""Open or reconcile the fail-closed Decodo billing-cycle ledger."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from mb_ceramics_catalogue.config.settings import Settings
from mb_ceramics_catalogue.proxy import (
    ProxyProfile,
    load_api_key,
    open_cycle,
    provider_usage,
    reconcile,
)


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("action", choices=("open", "reconcile"))
    command.add_argument("--dsn")
    command.add_argument("--api-secret", type=Path)
    command.add_argument("--cycle-start", type=datetime.fromisoformat)
    command.add_argument("--cycle-end", type=datetime.fromisoformat)
    return command


async def run() -> int:
    options = parser().parse_args()
    settings = Settings()
    dsn = options.dsn or settings.dsn
    secret = options.api_secret or settings.proxy_api_secret_file
    start = options.cycle_start or settings.proxy_billing_cycle_start
    end = options.cycle_end or settings.proxy_billing_cycle_end
    if not dsn or not secret or not start or not end:
        raise SystemExit("DSN, private API secret, and exact cycle start/end are required")
    profile = ProxyProfile("decodo", "", 0, "", "", api_key=load_api_key(secret))
    async with await psycopg.AsyncConnection.connect(
        dsn, row_factory=dict_row, autocommit=True
    ) as connection:
        try:
            reported = await provider_usage(profile, start, end)
        except Exception:
            if options.action == "reconcile":
                await reconcile(
                    connection, cycle_start=start,
                    provider_reported_bytes=0, successful=False,
                )
            raise
        if options.action == "open":
            await open_cycle(
                connection, cycle_start=start, cycle_end=end,
                provider_reported_bytes=reported,
            )
        else:
            await reconcile(
                connection, cycle_start=start,
                provider_reported_bytes=reported, successful=True,
            )
    print(f"decodo {options.action}: provider_reported_bytes={reported}")
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
