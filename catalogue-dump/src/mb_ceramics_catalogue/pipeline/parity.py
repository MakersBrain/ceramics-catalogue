"""Streaming, artifact-only shadow parity comparison.

The comparator never imports or calls collection code. Records are streamed
into a temporary SQLite database so input size is bounded by disk rather than
process memory and artifact ordering cannot affect the result.
"""

from __future__ import annotations

import gzip
import json
import math
import re
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

JsonObject = dict[str, Any]
Side = Literal["legacy", "connector"]
MISSING = object()
SENSITIVE_KEY = re.compile(
    r"(?:^|[_-])(?:access[_-]?token|token|secret|password|authorization|cookie|api[_-]?key)(?:$|[_-])",
    re.I,
)
BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{8,}=*")
INLINE_SECRET = re.compile(
    r"(?i)((?:access[_-]?token|token|secret|password|api[_-]?key)=)[^&\s]+"
)
MAX_AUXILIARY_JSON_BYTES = 1024 * 1024


class ArtifactComparisonError(ValueError):
    """Safe, user-facing invalid-input error."""


@dataclass(frozen=True, slots=True)
class NumericTolerance:
    absolute: float = 0.0
    relative: float = 0.0

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.absolute)
            or not math.isfinite(self.relative)
            or self.absolute < 0
            or self.relative < 0
        ):
            raise ArtifactComparisonError("numeric tolerances must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class ComparisonRules:
    """Reviewed semantic differences accepted by a shadow gate."""

    ignore_fields: frozenset[str] = frozenset()
    numeric_tolerances: Mapping[str, NumericTolerance] = field(default_factory=dict)
    metadata_ignore_fields: frozenset[str] = frozenset()

    @classmethod
    def from_json(cls, value: object) -> ComparisonRules:
        if not isinstance(value, dict):
            raise ArtifactComparisonError("rules must be a JSON object")
        allowed = {"version", "ignore_fields", "numeric_tolerances", "metadata_ignore_fields"}
        unknown = set(value) - allowed
        if unknown:
            raise ArtifactComparisonError(f"unknown rule keys: {', '.join(sorted(unknown))}")
        if value.get("version") != 1:
            raise ArtifactComparisonError("rules.version must be 1")
        ignore = _string_set(value.get("ignore_fields", []), "ignore_fields")
        metadata_ignore = _string_set(
            value.get("metadata_ignore_fields", []), "metadata_ignore_fields"
        )
        raw_tolerances = value.get("numeric_tolerances", {})
        if not isinstance(raw_tolerances, dict):
            raise ArtifactComparisonError("numeric_tolerances must be an object")
        tolerances: dict[str, NumericTolerance] = {}
        for path, raw in raw_tolerances.items():
            if not isinstance(path, str) or not path or not isinstance(raw, dict):
                raise ArtifactComparisonError("numeric tolerance entries must be path objects")
            if set(raw) - {"absolute", "relative"}:
                raise ArtifactComparisonError(f"unknown numeric tolerance keys for {path}")
            absolute = raw.get("absolute", 0.0)
            relative = raw.get("relative", 0.0)
            if (
                not isinstance(absolute, int | float)
                or isinstance(absolute, bool)
                or not isinstance(relative, int | float)
                or isinstance(relative, bool)
            ):
                raise ArtifactComparisonError(f"numeric tolerance for {path} must be numeric")
            tolerances[path] = NumericTolerance(float(absolute), float(relative))
        return cls(ignore, tolerances, metadata_ignore)


@dataclass(frozen=True, slots=True)
class ComparisonLimits:
    max_records_per_artifact: int = 5_000_000
    max_line_bytes: int = 8 * 1024 * 1024
    sample_limit: int = 20
    max_field_paths: int = 10_000

    def __post_init__(self) -> None:
        if (
            self.max_records_per_artifact < 1
            or self.max_line_bytes < 2
            or self.sample_limit < 0
            or self.max_field_paths < 1
        ):
            raise ArtifactComparisonError("comparison limits must be positive")


@dataclass(frozen=True, slots=True)
class ArtifactStats:
    name: str
    records: int
    bytes: int
    coverage: dict[str, int]


@dataclass(frozen=True, slots=True)
class ComparisonReport:
    equal: bool
    legacy: ArtifactStats
    connector: ArtifactStats
    added_count: int
    removed_count: int
    changed_count: int
    added_ids: tuple[str, ...]
    removed_ids: tuple[str, ...]
    changed_ids: tuple[str, ...]
    field_differences: dict[str, int]
    samples: tuple[JsonObject, ...]
    coverage_differences: dict[str, dict[str, int]]
    metadata: JsonObject
    rules: JsonObject

    def as_dict(self) -> JsonObject:
        return {
            "format": "catalogue.shadow_parity.v1",
            "equal": self.equal,
            "artifacts": {
                "legacy": _stats_dict(self.legacy),
                "connector": _stats_dict(self.connector),
            },
            "summary": {
                "added": self.added_count,
                "removed": self.removed_count,
                "changed": self.changed_count,
            },
            "added_ids": list(self.added_ids),
            "removed_ids": list(self.removed_ids),
            "changed_ids": list(self.changed_ids),
            "field_differences": self.field_differences,
            "samples": list(self.samples),
            "coverage_differences": self.coverage_differences,
            "metadata": self.metadata,
            "rules": self.rules,
        }


def load_rules(path: Path | None) -> ComparisonRules:
    if path is None:
        return ComparisonRules()
    _validate_auxiliary_size(path, "rules file")
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ArtifactComparisonError(f"rules file is unreadable: {type(error).__name__}") from error
    return ComparisonRules.from_json(value)


def load_metadata(path: Path | None, side: Side) -> JsonObject | None:
    if path is None:
        return None
    _validate_auxiliary_size(path, f"{side} metadata")
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ArtifactComparisonError(
            f"{side} metadata is unreadable: {type(error).__name__}"
        ) from error
    if not isinstance(value, dict):
        raise ArtifactComparisonError(f"{side} metadata must be a JSON object")
    return value


def compare_artifacts(
    legacy_path: Path,
    connector_path: Path,
    *,
    identity_field: str = "external_id",
    rules: ComparisonRules | None = None,
    limits: ComparisonLimits | None = None,
    legacy_metadata: JsonObject | None = None,
    connector_metadata: JsonObject | None = None,
) -> ComparisonReport:
    """Compare existing artifacts, translating infrastructure failures safely."""

    try:
        return _compare_artifacts(
            legacy_path,
            connector_path,
            identity_field=identity_field,
            rules=rules,
            limits=limits,
            legacy_metadata=legacy_metadata,
            connector_metadata=connector_metadata,
        )
    except ArtifactComparisonError:
        raise
    except (OSError, sqlite3.Error) as error:
        raise ArtifactComparisonError(
            f"artifact comparison failed: {type(error).__name__}"
        ) from error


def _compare_artifacts(
    legacy_path: Path,
    connector_path: Path,
    *,
    identity_field: str = "external_id",
    rules: ComparisonRules | None = None,
    limits: ComparisonLimits | None = None,
    legacy_metadata: JsonObject | None = None,
    connector_metadata: JsonObject | None = None,
) -> ComparisonReport:
    """Compare existing NDJSON artifacts without performing collection work."""

    if not identity_field or any(part == "" for part in identity_field.split(".")):
        raise ArtifactComparisonError("identity_field must be a non-empty dotted path")
    active_rules = rules or ComparisonRules()
    active_limits = limits or ComparisonLimits()
    _validate_artifact(legacy_path, "legacy")
    _validate_artifact(connector_path, "connector")
    with tempfile.TemporaryDirectory(prefix="catalogue-parity-") as directory:
        database = Path(directory) / "records.sqlite3"
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "create table records (side text not null, identity text not null, payload text not null, "
                "primary key (side, identity)) without rowid"
            )
            legacy = _ingest(
                connection, legacy_path, "legacy", identity_field, active_rules, active_limits
            )
            connector = _ingest(
                connection, connector_path, "connector", identity_field, active_rules, active_limits
            )
            connection.commit()
            return _compare(
                connection,
                legacy,
                connector,
                active_rules,
                active_limits,
                legacy_metadata,
                connector_metadata,
            )
        finally:
            connection.close()


def _ingest(
    connection: sqlite3.Connection,
    path: Path,
    side: Side,
    identity_field: str,
    rules: ComparisonRules,
    limits: ComparisonLimits,
) -> ArtifactStats:
    coverage: Counter[str] = Counter()
    records = 0
    for line_number, record in _records(path, side, limits):
        identity = _lookup(record, identity_field)
        if not isinstance(identity, str | int) or isinstance(identity, bool) or not str(identity):
            raise ArtifactComparisonError(
                f"{side} line {line_number} has no scalar {identity_field}"
            )
        key = str(identity)
        normalized = _without_ignored(record, rules.ignore_fields)
        for field_path, value in _flatten(record).items():
            if _present(value):
                coverage[field_path] += 1
        if len(coverage) > limits.max_field_paths:
            raise ArtifactComparisonError(
                f"{side} artifact exceeds {limits.max_field_paths} distinct field paths"
            )
        try:
            connection.execute(
                "insert into records(side, identity, payload) values (?, ?, ?)",
                (side, key, _canonical_json(normalized)),
            )
        except sqlite3.IntegrityError as error:
            raise ArtifactComparisonError(
                f"{side} line {line_number} duplicates a stable identity"
            ) from error
        records += 1
    return ArtifactStats(side, records, path.stat().st_size, dict(sorted(coverage.items())))


def _records(path: Path, side: Side, limits: ComparisonLimits) -> Iterator[tuple[int, JsonObject]]:
    opener = gzip.open if path.suffix.casefold() == ".gz" else open
    try:
        with opener(path, "rb") as handle:
            line_number = 0
            record_count = 0
            while raw_line := handle.readline(limits.max_line_bytes + 1):
                line_number += 1
                if len(raw_line) > limits.max_line_bytes:
                    raise ArtifactComparisonError(
                        f"{side} line {line_number} exceeds {limits.max_line_bytes} bytes"
                    )
                if not raw_line.strip():
                    continue
                record_count += 1
                if record_count > limits.max_records_per_artifact:
                    raise ArtifactComparisonError(
                        f"{side} artifact exceeds {limits.max_records_per_artifact} records"
                    )
                try:
                    value = json.loads(raw_line)
                except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
                    raise ArtifactComparisonError(
                        f"{side} line {line_number} is malformed JSON"
                    ) from error
                if not isinstance(value, dict):
                    raise ArtifactComparisonError(f"{side} line {line_number} is not an object")
                yield line_number, value
    except ArtifactComparisonError:
        raise
    except (OSError, EOFError, gzip.BadGzipFile) as error:
        raise ArtifactComparisonError(
            f"{side} artifact is unreadable: {type(error).__name__}"
        ) from error


def _compare(
    connection: sqlite3.Connection,
    legacy: ArtifactStats,
    connector: ArtifactStats,
    rules: ComparisonRules,
    limits: ComparisonLimits,
    legacy_metadata: JsonObject | None,
    connector_metadata: JsonObject | None,
) -> ComparisonReport:
    added_count, added = _identities(connection, "connector", "legacy", limits.sample_limit)
    removed_count, removed = _identities(connection, "legacy", "connector", limits.sample_limit)
    field_counts: Counter[str] = Counter()
    samples: list[JsonObject] = []
    changed_ids: list[str] = []
    changed_count = 0
    rows = connection.execute(
        "select l.identity, l.payload, c.payload from records l join records c "
        "on c.identity=l.identity where l.side='legacy' and c.side='connector' "
        "order by l.identity"
    )
    for identity, legacy_payload, connector_payload in rows:
        left = json.loads(legacy_payload)
        right = json.loads(connector_payload)
        differences = _field_differences(left, right, rules.numeric_tolerances)
        if not differences:
            continue
        changed_count += 1
        if len(changed_ids) < limits.sample_limit:
            changed_ids.append(identity)
        field_counts.update(differences.keys())
        if len(samples) < limits.sample_limit:
            samples.append(
                {
                    "id": _redact_scalar(identity),
                    "fields": {
                        path: {
                            "legacy": _redact_value(values[0], path),
                            "connector": _redact_value(values[1], path),
                        }
                        for path, values in sorted(differences.items())
                    },
                }
            )
    metadata = _metadata_report(legacy_metadata, connector_metadata, rules)
    coverage = {
        path: {
            "legacy": legacy.coverage.get(path, 0),
            "connector": connector.coverage.get(path, 0),
            "delta": connector.coverage.get(path, 0) - legacy.coverage.get(path, 0),
        }
        for path in sorted(set(legacy.coverage) | set(connector.coverage))
        if legacy.coverage.get(path, 0) != connector.coverage.get(path, 0)
    }
    equal = not added_count and not removed_count and not changed_count and metadata.get("equal", True)
    return ComparisonReport(
        equal=equal,
        legacy=legacy,
        connector=connector,
        added_count=added_count,
        removed_count=removed_count,
        changed_count=changed_count,
        added_ids=tuple(_redact_scalar(value) for value in added),
        removed_ids=tuple(_redact_scalar(value) for value in removed),
        changed_ids=tuple(_redact_scalar(value) for value in changed_ids),
        field_differences=dict(sorted(field_counts.items())),
        samples=tuple(samples),
        coverage_differences=coverage,
        metadata=metadata,
        rules={
            "ignore_fields": sorted(rules.ignore_fields),
            "numeric_tolerances": {
                path: {"absolute": value.absolute, "relative": value.relative}
                for path, value in sorted(rules.numeric_tolerances.items())
            },
            "metadata_ignore_fields": sorted(rules.metadata_ignore_fields),
        },
    )


def _identities(
    connection: sqlite3.Connection, side: Side, absent: Side, sample_limit: int
) -> tuple[int, list[str]]:
    count = 0
    samples: list[str] = []
    rows = connection.execute(
        "select identity from records r where side=? and not exists "
        "(select 1 from records x where x.side=? and x.identity=r.identity) order by identity",
        (side, absent),
    )
    for row in rows:
        count += 1
        if len(samples) < sample_limit:
            samples.append(row[0])
    return count, samples


def _field_differences(
    left: JsonObject,
    right: JsonObject,
    tolerances: Mapping[str, NumericTolerance],
) -> dict[str, tuple[Any, Any]]:
    left_fields = _flatten(left)
    right_fields = _flatten(right)
    differences: dict[str, tuple[Any, Any]] = {}
    for path in sorted(set(left_fields) | set(right_fields)):
        first = left_fields.get(path, MISSING)
        second = right_fields.get(path, MISSING)
        if _equivalent(first, second, tolerances.get(path)):
            continue
        differences[path] = (first, second)
    return differences


def _equivalent(first: Any, second: Any, tolerance: NumericTolerance | None) -> bool:
    if first is MISSING or second is MISSING:
        return first is second
    if first == second:
        return True
    if (
        tolerance is not None
        and isinstance(first, int | float)
        and not isinstance(first, bool)
        and isinstance(second, int | float)
        and not isinstance(second, bool)
    ):
        difference = abs(float(first) - float(second))
        allowed = tolerance.absolute + tolerance.relative * max(abs(float(first)), abs(float(second)))
        return math.isfinite(difference) and difference <= allowed
    return False


def _metadata_report(
    legacy: JsonObject | None, connector: JsonObject | None, rules: ComparisonRules
) -> JsonObject:
    if legacy is None and connector is None:
        return {"provided": False, "equal": True, "differences": {}}
    left = _without_ignored(legacy or {}, rules.metadata_ignore_fields)
    right = _without_ignored(connector or {}, rules.metadata_ignore_fields)
    differences = _field_differences(left, right, {})
    return {
        "provided": True,
        "equal": not differences,
        "legacy": _redact_value(left),
        "connector": _redact_value(right),
        "differences": {
            path: {
                "legacy": _redact_value(values[0], path),
                "connector": _redact_value(values[1], path),
            }
            for path, values in sorted(differences.items())
        },
    }


def _flatten(value: JsonObject, prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            if item:
                result.update(_flatten(item, path))
            else:
                result[path] = item
        else:
            result[path] = item
    return result


def _without_ignored(value: JsonObject, ignored: frozenset[str]) -> JsonObject:
    def visit(item: Any, path: str) -> Any:
        if isinstance(item, dict):
            return {
                key: visit(child, f"{path}.{key}" if path else key)
                for key, child in item.items()
                if not _ignored(f"{path}.{key}" if path else key, ignored)
            }
        if isinstance(item, list):
            return [visit(child, path) for child in item]
        return item

    return visit(value, "")


def _ignored(path: str, ignored: frozenset[str]) -> bool:
    return any(path == candidate or path.startswith(candidate + ".") for candidate in ignored)


def _lookup(value: JsonObject, path: str) -> Any:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return MISSING
        current = current[part]
    return current


def _present(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _canonical_json(value: JsonObject) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as error:
        raise ArtifactComparisonError("artifact contains a non-finite or invalid JSON value") from error


def _redact_value(value: Any, key: str = "") -> Any:
    if value is MISSING:
        return {"missing": True}
    if SENSITIVE_KEY.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(name): _redact_value(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, key) for item in value[:20]]
    if isinstance(value, str):
        return _redact_scalar(value)
    return value


def _redact_scalar(value: str) -> str:
    safe = INLINE_SECRET.sub(r"\1[redacted]", BEARER.sub("Bearer [redacted]", value))
    if Path(safe).is_absolute() or re.match(r"^[A-Za-z]:[\\/]", safe):
        return "[path redacted]"
    parsed = urlsplit(safe)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        safe = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return safe[:500]


def _string_set(value: object, name: str) -> frozenset[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ArtifactComparisonError(f"{name} must be an array of non-empty strings")
    return frozenset(value)


def _validate_artifact(path: Path, side: Side) -> None:
    if path.suffix.casefold() not in {".ndjson", ".gz"}:
        raise ArtifactComparisonError(f"{side} artifact must be .ndjson or .ndjson.gz")
    if not path.is_file():
        raise ArtifactComparisonError(f"{side} artifact is not a readable file")
    if path.suffix.casefold() == ".gz" and not path.name.casefold().endswith(".ndjson.gz"):
        raise ArtifactComparisonError(f"{side} gzip artifact must end in .ndjson.gz")


def _stats_dict(value: ArtifactStats) -> JsonObject:
    return {
        "name": value.name,
        "records": value.records,
        "bytes": value.bytes,
        "coverage": value.coverage,
    }


def _validate_auxiliary_size(path: Path, label: str) -> None:
    try:
        size = path.stat().st_size
    except OSError as error:
        raise ArtifactComparisonError(f"{label} is unreadable: {type(error).__name__}") from error
    if size > MAX_AUXILIARY_JSON_BYTES:
        raise ArtifactComparisonError(f"{label} exceeds {MAX_AUXILIARY_JSON_BYTES} bytes")
