"""Compare the immutable NDJSON artifacts produced by two source jobs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

# Collection time and scraper evidence are useful in the artifact, but they are
# not catalogue changes. Comparing them would mark every row as changed on every
# run and bury the price/name/availability changes an operator is looking for.
IGNORED_FIELDS = {"fetched_at", "raw"}
IDENTITY_FIELDS = {"format", "source", "external_id"}


class ArtifactError(ValueError):
    """An artifact cannot safely be used for a comparison."""


def read_artifact(root: Path, recorded: str, expected_sha256: str | None) -> dict[str, dict[str, Any]]:
    """Read one artifact, constrained to the configured read-only artifact root."""
    base = root.resolve()
    candidate = Path(recorded)
    path = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
    try:
        path.relative_to(base)
    except ValueError as error:
        raise ArtifactError("artifact path is outside the configured artifact directory") from error

    try:
        body = path.read_bytes()
    except OSError as error:
        raise ArtifactError(f"artifact is unavailable: {error.strerror or error}") from error

    digest = hashlib.sha256(body).hexdigest()
    if expected_sha256 and digest != expected_sha256:
        raise ArtifactError("artifact checksum does not match the job record")

    records: dict[str, dict[str, Any]] = {}
    for number, raw_line in enumerate(body.splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            record = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ArtifactError(f"artifact contains invalid JSON on line {number}") from error
        external_id = record.get("external_id") if isinstance(record, dict) else None
        if not isinstance(external_id, str) or not external_id:
            raise ArtifactError(f"artifact record on line {number} has no external_id")
        if external_id in records:
            raise ArtifactError(f"artifact contains duplicate external_id {external_id!r}")
        records[external_id] = record
    return records


def compare(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    *,
    kind: str | None = None,
    search: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Return counts plus a bounded, searchable list of semantic changes."""
    changes: list[dict[str, Any]] = []
    counts = {"added": 0, "removed": 0, "changed": 0, "unchanged": 0}

    for external_id in sorted(before.keys() | after.keys()):
        old = before.get(external_id)
        new = after.get(external_id)
        change: dict[str, Any]
        if old is None:
            change = {"kind": "added", "external_id": external_id, "name": _name(new)}
        elif new is None:
            change = {"kind": "removed", "external_id": external_id, "name": _name(old)}
        else:
            fields = _changed_fields(old, new)
            if not fields:
                counts["unchanged"] += 1
                continue
            change = {
                "kind": "changed",
                "external_id": external_id,
                "name": _name(new),
                "fields": fields,
            }
        change_kind = str(change["kind"])
        counts[change_kind] += 1
        changes.append(change)

    needle = (search or "").strip().casefold()
    matched = [
        change
        for change in changes
        if (kind is None or change["kind"] == kind)
        and (
            not needle
            or needle in change["external_id"].casefold()
            or needle in (change.get("name") or "").casefold()
        )
    ]
    return {**counts, "matched": len(matched), "items": matched[:limit]}


def _name(record: dict[str, Any] | None) -> str | None:
    value = (record or {}).get("name")
    return value if isinstance(value, str) else None


def _changed_fields(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    fields = (before.keys() | after.keys()) - IGNORED_FIELDS - IDENTITY_FIELDS
    return [
        {"field": field, "before": before.get(field), "after": after.get(field)}
        for field in sorted(fields)
        if before.get(field) != after.get(field)
    ]
