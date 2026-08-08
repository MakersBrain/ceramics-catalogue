"""`catalogue-ops-openapi`: write or verify the operator contract.

    catalogue-ops-openapi              write catalogue-ops.openapi.json
    catalogue-ops-openapi --check      fail if the checked-in file has drifted
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from catalogue_control.spec import registry

DEFAULT_TARGET = Path(__file__).resolve().parents[2] / "catalogue-ops.openapi.json"


def main() -> int:
    parser = argparse.ArgumentParser(prog="catalogue-ops-openapi", description=__doc__)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--check", action="store_true", help="verify rather than write")
    options = parser.parse_args()

    api = registry()
    if options.check:
        if problems := api.check(options.target):
            print(f"catalogue-ops-openapi: {options.target} is out of date:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            print("\nRun `make openapi` and commit the result.", file=sys.stderr)
            return 1
        print(f"{options.target} is up to date")
        return 0

    changed = api.write(options.target)
    print(f"{'wrote' if changed else 'unchanged'} {options.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
