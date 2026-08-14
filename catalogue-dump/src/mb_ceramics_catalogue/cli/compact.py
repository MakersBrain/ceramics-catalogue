"""Report or execute bounded legacy PostgreSQL observation compaction."""

from __future__ import annotations

import argparse
import json

from mb_ceramics_catalogue.ops import compaction, retention
from mb_ceramics_catalogue.storage.db import dsn_from_environment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default="")
    parser.add_argument("--batch", type=int, default=100)
    parser.add_argument("--max-batches", type=int, default=1)
    parser.add_argument("--execute", action="store_true")
    options = parser.parse_args()
    totals = compaction.CompactReport()
    with retention.connect(dsn_from_environment(options.dsn)) as connection:
        for _ in range(max(1, options.max_batches)):
            current = compaction.compact(connection, max(1, options.batch), execute=options.execute)
            totals = compaction.CompactReport(
                totals.raw_deleted + current.raw_deleted,
                totals.offers_deleted + current.offers_deleted,
            )
            if not options.execute or not (current.raw_deleted or current.offers_deleted):
                break
    print(json.dumps({
        "mode": "execute" if options.execute else "dry-run",
        "raw_deleted": totals.raw_deleted,
        "offers_deleted": totals.offers_deleted,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
