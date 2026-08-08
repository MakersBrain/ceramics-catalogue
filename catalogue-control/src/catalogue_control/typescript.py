"""`catalogue-ops-types`: emit the explorer's TypeScript from the ops document.

`catalogue-explorer/src/lib/catalogue.ts` hand-declares `Product` with 34 fields
and a comment explaining why it lives where it does. That is a drift source that
fails *silently*: a renamed column compiles fine and renders blank.

A deliberately small generator rather than `openapi-typescript`: the document is
generated from Pydantic models with no unions of unions and no `allOf`
gymnastics, and one file of readable output beats a Node toolchain in the Python
build. If the document ever needs more than this, the answer is the real tool,
not more of this one.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from catalogue_control.spec import registry

DEFAULT_TARGET = (
    Path(__file__).resolve().parents[3]
    / "catalogue-explorer" / "src" / "lib" / "ops" / "types.ts"
)

HEADER = """/**
 * GENERATED FILE — do not edit.
 *
 * Produced from catalogue-ops.openapi.json by `catalogue-ops-types`, which is
 * generated in turn from the Pydantic models catalogue-control serialises with.
 * Edit those; run `make openapi` and `make types`; commit the result.
 *
 * The drift this removes is the silent kind: a renamed field compiles fine
 * against a hand-written interface and renders blank.
 */

"""


def to_type(schema: dict[str, Any], required: bool) -> str:
    rendered = _bare(schema)
    if not required and "null" not in rendered:
        rendered = f"{rendered} | null"
    return rendered


def _bare(schema: dict[str, Any]) -> str:
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if "anyOf" in schema:
        parts = [_bare(option) for option in schema["anyOf"]]
        # `T | null` reads better than `T | "null"`, and pydantic emits the null
        # branch as its own `{"type": "null"}`.
        return " | ".join(dict.fromkeys(parts))
    if "enum" in schema:
        return " | ".join(f"'{value}'" for value in schema["enum"])
    if "const" in schema:
        return f"'{schema['const']}'"

    kind = schema.get("type")
    if not isinstance(kind, str):
        # A schema with no `type` at all — pydantic emits these for `Any`. There
        # is nothing honest to write but `unknown`.
        return "unknown"
    if kind == "array":
        return f"{_bare(schema.get('items', {}))}[]"
    if kind == "object":
        values = schema.get("additionalProperties")
        inner = _bare(values) if isinstance(values, dict) and values else "unknown"
        return f"Record<string, {inner}>"
    return {
        "string": "string",
        "integer": "number",
        "number": "number",
        "boolean": "boolean",
        "null": "null",
    }.get(kind, "unknown")


def render(document: dict[str, Any]) -> str:
    lines = [HEADER]
    schemas = document.get("components", {}).get("schemas", {})

    for name in sorted(schemas):
        schema = schemas[name]
        if schema.get("type") != "object" and "properties" not in schema:
            continue
        required = set(schema.get("required", []))
        if description := schema.get("description"):
            lines.append(f"/** {description.splitlines()[0]} */")
        lines.append(f"export interface {name} {{")
        for field, definition in schema.get("properties", {}).items():
            optional = field not in required
            if note := definition.get("description"):
                lines.append(f"\t/** {' '.join(note.split())} */")
            lines.append(f"\t{field}{'?' if optional else ''}: {to_type(definition, field in required)};")
        lines.append("}")
        lines.append("")

    # Aliases so the explorer's existing imports keep working: the hand-written
    # file used these names, and renaming call sites would defeat the point of
    # generating a drop-in replacement.
    lines.extend(
        [
            "// Names the explorer already imports, mapped onto the generated ones.",
            "export type RunRow = Run;",
            "export type WorkerRow = Worker;",
            "export type SourceRow = Source;",
            "export type NotificationRow = Notification;",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(prog="catalogue-ops-types", description=__doc__)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--check", action="store_true")
    options = parser.parse_args()

    rendered = render(registry().build())

    if options.check:
        if not options.target.exists():
            print(f"catalogue-ops-types: {options.target} does not exist", file=sys.stderr)
            return 1
        if options.target.read_text(encoding="utf-8") != rendered:
            print(
                f"catalogue-ops-types: {options.target} is out of date; run `make types`",
                file=sys.stderr,
            )
            return 1
        print(f"{options.target} is up to date")
        return 0

    options.target.parent.mkdir(parents=True, exist_ok=True)
    options.target.write_text(rendered, encoding="utf-8")
    print(f"wrote {options.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
