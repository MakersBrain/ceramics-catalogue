"""`catalogue-source-health`: take a source out of runs until its site recovers.

The operator half of `ops.health`. Disabling a source by hand is one statement;
disabling it *and* recording how to tell when the supplier is back is the part
worth having a command for, because the second half is the one that gets
forgotten and a source silently missing from the catalogue is the expensive
failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from mb_ceramics_catalogue.ops import health
from mb_ceramics_catalogue.storage.db import connect, dsn_from_environment


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="catalogue-source-health", description=__doc__)
    command.add_argument("--dsn", default="")
    sub = command.add_subparsers(dest="action", required=True)

    disable = sub.add_parser("disable", help="take a source out of runs and probe for recovery")
    disable.add_argument("source")
    disable.add_argument("--url", required=True, help="what to request to tell whether it is back")
    disable.add_argument("--reason", required=True)
    disable.add_argument(
        "--expect", choices=("json", "ok"), default="json",
        help="'json' also requires a parseable body, which a caching layer answering 200 fails",
    )
    disable.add_argument(
        "--required-ok", type=int, default=2,
        help="consecutive successful probes before the source goes back into runs",
    )

    sub.add_parser("list", help="what is currently out of runs, and why")
    sub.add_parser("check", help="run one probe pass now instead of waiting for the leader")
    return command


async def run(options: argparse.Namespace) -> int:
    async with connect(dsn_from_environment(options.dsn)) as connection:
        if options.action == "disable":
            await health.disable(
                connection,
                options.source,
                url=options.url,
                reason=options.reason,
                expect=options.expect,
                required_ok=options.required_ok,
            )
            print(json.dumps({"disabled": options.source, "probe": options.url}, indent=2))
            return 0

        if options.action == "check":
            recovered = await health.check_recovered(connection)
            print(json.dumps({"recovered": recovered}, indent=2))
            return 0

        cursor = await connection.execute(
            """
            select p.source_id, p.url, p.reason, p.expect, p.disabled_at, p.last_checked_at,
                   p.last_status, p.last_error, p.checks, p.consecutive_ok, p.required_ok,
                   p.recovered_at, coalesce(s.enabled, true) as enabled
              from catalogue.source_health_probes p
              left join catalogue.source_settings s on s.source_id = p.source_id
             order by p.recovered_at nulls first, p.source_id
            """
        )
        rows = await cursor.fetchall()
        print(json.dumps(rows, indent=2, sort_keys=True, default=str))
        return 0


def main() -> int:
    return asyncio.run(run(parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
