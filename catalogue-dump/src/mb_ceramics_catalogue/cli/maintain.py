"""Report or execute bounded artifact and response-cache retention."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mb_ceramics_catalogue.ops import retention
from mb_ceramics_catalogue.scrapers.cache import prune
from mb_ceramics_catalogue.storage.db import dsn_from_environment


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="catalogue-maintain", description=__doc__)
    command.add_argument("--dsn", default="")
    command.add_argument("--artifacts", type=Path, required=True)
    command.add_argument("--cache", type=Path, required=True)
    command.add_argument("--cache-max-age-days", type=float, default=30.0)
    command.add_argument("--cache-max-gb", type=float, default=5.0)
    command.add_argument("--execute", action="store_true")
    return command


def main() -> int:
    options = parser().parse_args()
    with retention.connect(dsn_from_environment(options.dsn)) as connection:
        artifacts = retention.select(connection, options.artifacts)
        cache = prune(
            options.cache,
            max_age_seconds=options.cache_max_age_days * 86400,
            max_bytes=int(options.cache_max_gb * 1_000_000_000),
            dry_run=not options.execute,
        )
        if options.execute:
            retention.execute(connection, artifacts)
    print(json.dumps({
        "mode": "execute" if options.execute else "dry-run",
        "artifacts": {"files": artifacts.files, "bytes": artifacts.bytes,
                      "jobs": [target.job_id for target in artifacts.targets]},
        "cache": {"files": cache.files, "bytes": cache.bytes},
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
