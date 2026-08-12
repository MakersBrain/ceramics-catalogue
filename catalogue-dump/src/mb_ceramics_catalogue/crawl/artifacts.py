"""Writing a source's rows to disk, and the manifest that describes the run.

Moved from `dump.py` unchanged in behaviour. These were already the right
functions; they were simply living inside the module that decides what to crawl.

The rule they encode is the important part, and it survives verbatim: **an empty
or interrupted run never replaces a good file**. A run that stopped halfway is
not a smaller catalogue, it is an incomplete one, and letting it overwrite a
complete dump would quietly delete products that are still for sale — and then
the loader, seeing a complete-looking file, would retire them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RECORD_FORMAT = "ceramics.catalogue_item.v2"
DUMP_FORMAT = "ceramics.catalogue_dump.v2"


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _ndjson(records: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(record, ensure_ascii=False, default=str) + "\n" for record in records)


@dataclass(frozen=True)
class Artifact:
    """Where a source's rows landed, and what they hash to.

    The digest and size exist for `catalogue.jobs`: an artifact recorded in the
    database with neither is a path that may or may not still hold what the run
    produced, which is not an audit trail.
    """

    path: Path
    sha256: str
    size: int
    status: str


def write_source(
    output: Path, name: str, records: list[dict[str, Any]], allow_empty: bool = False
) -> Artifact:
    """Replace a source dump atomically, never losing data to an empty run."""
    target = output / f"{name}.ndjson"
    existing = target.exists() and target.stat().st_size > 0
    if not (records or allow_empty or not existing):
        return Artifact(target, "", target.stat().st_size, "preserved_existing_nonempty")
    return _write(target, records, "replaced")


def write_partial(output: Path, name: str, records: list[dict[str, Any]]) -> Artifact:
    """Keep an interrupted source's rows beside the dump, never as the dump."""
    target = output / f"{name}.partial.ndjson"
    if not records:
        return Artifact(target, "", 0, "skipped_empty")
    return _write(target, records, "partial")


def _write(target: Path, records: list[dict[str, Any]], status: str) -> Artifact:
    """Write beside the target and move it into place.

    An atomic rename is what stops a killed process — `docker stop`, an OOM,
    Ctrl-C — from leaving a half-written file that the loader would then read as
    a complete catalogue and retire against.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    body = _ndjson(records)
    encoded = body.encode("utf-8")
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(target)
    return Artifact(target, hashlib.sha256(encoded).hexdigest(), len(encoded), status)


def job_directory(root: Path, run_id: str, job_id: str) -> Path:
    """`dumps/<run-id>/<job-id>/`, the namespace one job's artifacts live in.

    Never a shared source-only name. Two runs of the same source must not write
    the same path, or the second silently destroys the first's evidence and the
    `artifact_sha256` recorded against the first job stops describing anything.
    """
    return root / run_id / job_id


class Manifest:
    """What a run collected, per source, written once at the end.

    `plan_load` reads this to decide whether a complete-looking file may be
    treated as the whole of a supplier's catalogue, so the `write_status` and
    `truncated` fields it carries are load-bearing rather than informational.
    """

    def __init__(self, started_at: str | None = None) -> None:
        self.data: dict[str, Any] = {
            "format": DUMP_FORMAT,
            "started_at": started_at or now(),
            "record_format": RECORD_FORMAT,
            "sources": {},
        }

    def record(self, name: str, summary: dict[str, Any]) -> None:
        self.data["sources"][name] = summary

    def finish(self, record_count: int) -> dict[str, Any]:
        self.data["finished_at"] = now()
        self.data["record_count"] = record_count
        return self.data

    def write(self, output: Path) -> Path:
        target = output / "manifest.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
        return target
