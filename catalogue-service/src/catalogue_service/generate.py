"""`catalogue-openapi`: write or verify the generated contract.

    catalogue-openapi              write catalogue.openapi.json
    catalogue-openapi --check      fail if the checked-in file has drifted

The `--check` mode is what CI runs. Without it, "regenerate it later" becomes
"the document is six months old and nobody trusts it".
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ateliera_catalogue.contracts import assert_read_only

from catalogue_service.spec import registry

DEFAULT_TARGET = Path(__file__).resolve().parents[2] / "catalogue.openapi.json"


def main() -> int:
    parser = argparse.ArgumentParser(prog="catalogue-openapi", description=__doc__)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--check", action="store_true", help="verify rather than write")
    options = parser.parse_args()

    api = registry()
    document = api.build()

    # The read-only property, asserted rather than promised in a docstring.
    if offenders := assert_read_only(document):
        print(
            "catalogue-openapi: the read API must expose no write path, but the "
            f"registry declares {', '.join(offenders)}",
            file=sys.stderr,
        )
        return 1

    if options.check:
        if problems := api.check(options.target):
            print(f"catalogue-openapi: {options.target} is out of date:", file=sys.stderr)
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
