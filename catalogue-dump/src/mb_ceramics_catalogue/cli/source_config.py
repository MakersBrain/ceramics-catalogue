"""Inspect deterministic typed projections of legacy source configuration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mb_ceramics_catalogue.config.source_projection import inspect_sources
from mb_ceramics_catalogue.config.sources import SourcesFile, default_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources-file", type=Path, default=default_path())
    parser.add_argument("--strict", action="store_true", help="fail when findings are present")
    args = parser.parse_args()
    reports = inspect_sources(SourcesFile.load(args.sources_file))
    print(json.dumps([item.model_dump(mode="json") for item in reports], indent=2, sort_keys=True))
    findings = any(item.ambiguous_fields or item.unused_fields for item in reports)
    return 1 if args.strict and findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
