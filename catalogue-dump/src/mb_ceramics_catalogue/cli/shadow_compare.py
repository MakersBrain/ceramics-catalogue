"""Compare existing legacy and connector artifacts without crawling either source."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mb_ceramics_catalogue.pipeline.parity import (
    ArtifactComparisonError,
    ComparisonLimits,
    compare_artifacts,
    load_metadata,
    load_rules,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="catalogue-shadow-compare", description=__doc__)
    parser.add_argument("legacy", type=Path, help="existing legacy .ndjson or .ndjson.gz")
    parser.add_argument("connector", type=Path, help="existing connector .ndjson or .ndjson.gz")
    parser.add_argument("--rules", type=Path, help="reviewed version-1 JSON ignore/tolerance rules")
    parser.add_argument("--legacy-metadata", type=Path, help="optional existing legacy summary JSON")
    parser.add_argument("--connector-metadata", type=Path, help="optional connector summary JSON")
    parser.add_argument("--identity-field", default="external_id", help="stable dotted identity path")
    parser.add_argument("--samples", type=int, default=20, help="maximum deterministic samples")
    parser.add_argument("--max-records", type=int, default=5_000_000, help="per-artifact bound")
    parser.add_argument("--max-line-bytes", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--max-field-paths", type=int, default=10_000)
    parser.add_argument("--pretty", action="store_true", help="indent JSON for operator inspection")
    return parser


def run(options: argparse.Namespace) -> int:
    rules = load_rules(options.rules)
    limits = ComparisonLimits(
        max_records_per_artifact=options.max_records,
        max_line_bytes=options.max_line_bytes,
        sample_limit=options.samples,
        max_field_paths=options.max_field_paths,
    )
    report = compare_artifacts(
        options.legacy,
        options.connector,
        identity_field=options.identity_field,
        rules=rules,
        limits=limits,
        legacy_metadata=load_metadata(options.legacy_metadata, "legacy"),
        connector_metadata=load_metadata(options.connector_metadata, "connector"),
    )
    print(
        json.dumps(
            report.as_dict(),
            ensure_ascii=False,
            sort_keys=True,
            indent=2 if options.pretty else None,
            separators=None if options.pretty else (",", ":"),
        )
    )
    return 0 if report.equal else 1


def main() -> int:
    options = build_parser().parse_args()
    try:
        return run(options)
    except ArtifactComparisonError as error:
        print(f"catalogue-shadow-compare: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
