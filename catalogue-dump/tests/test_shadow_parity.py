from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import pytest

from mb_ceramics_catalogue.cli import shadow_compare
from mb_ceramics_catalogue.pipeline.parity import (
    ArtifactComparisonError,
    ComparisonLimits,
    ComparisonRules,
    NumericTolerance,
    compare_artifacts,
)


def write(path: Path, records: list[object], *, gzip_output: bool = False) -> Path:
    content = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    if gzip_output:
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def record(identifier: str, price: float = 12.5, **extra: object) -> dict[str, object]:
    return {
        "external_id": identifier,
        "name": f"Product {identifier}",
        "price": price,
        "currency": "EUR",
        **extra,
    }


def test_order_independence_and_metadata_reporting(tmp_path: Path) -> None:
    first = record("one")
    second = record("two")
    legacy = write(tmp_path / "legacy.ndjson", [first, second])
    connector = write(tmp_path / "connector.ndjson", [second, first])

    report = compare_artifacts(
        legacy,
        connector,
        legacy_metadata={"records": 2, "requests": 4, "artifact_path": "/private/legacy"},
        connector_metadata={"requests": 4, "records": 2, "artifact_path": "/private/legacy"},
    )

    assert report.equal
    assert report.metadata["equal"] is True
    assert report.metadata["legacy"]["artifact_path"] == "[path redacted]"
    assert report.legacy.coverage["price"] == 2
    assert report.coverage_differences == {}


def test_duplicate_identity_is_rejected_with_line_but_no_record_body(tmp_path: Path) -> None:
    secret = "do-not-print-this"
    legacy = write(tmp_path / "legacy.ndjson", [record("one"), record("one", secret=secret)])
    connector = write(tmp_path / "connector.ndjson", [])

    with pytest.raises(ArtifactComparisonError) as caught:
        compare_artifacts(legacy, connector)

    assert "line 2" in str(caught.value)
    assert secret not in str(caught.value)


@pytest.mark.parametrize(
    "content,message",
    [("not-json\n", "malformed JSON"), ("[]\n", "not an object"), ('{"name":"x"}\n', "external_id")],
)
def test_malformed_input_is_a_safe_error(
    tmp_path: Path, content: str, message: str
) -> None:
    legacy = tmp_path / "legacy.ndjson"
    legacy.write_text(content, encoding="utf-8")
    connector = write(tmp_path / "connector.ndjson", [])

    with pytest.raises(ArtifactComparisonError, match=message):
        compare_artifacts(legacy, connector)


def test_gzip_artifact_is_streamed(tmp_path: Path) -> None:
    legacy = write(tmp_path / "legacy.ndjson.gz", [record("one")], gzip_output=True)
    connector = write(tmp_path / "connector.ndjson", [record("one")])

    assert compare_artifacts(legacy, connector).equal


def test_reviewed_volatile_ignore_and_numeric_tolerance(tmp_path: Path) -> None:
    legacy = write(
        tmp_path / "legacy.ndjson",
        [record("one", 10.0, fetched_at="2026-01-01T00:00:00Z", stock={"quantity": 5})],
    )
    connector = write(
        tmp_path / "connector.ndjson",
        [record("one", 10.009, fetched_at="2026-08-15T00:00:00Z", stock={"quantity": 5})],
    )
    rules = ComparisonRules(
        ignore_fields=frozenset({"fetched_at"}),
        numeric_tolerances={"price": NumericTolerance(absolute=0.01)},
    )

    assert compare_artifacts(legacy, connector, rules=rules).equal

    changed = compare_artifacts(
        legacy,
        connector,
        rules=ComparisonRules(ignore_fields=frozenset({"fetched_at"})),
    )
    assert not changed.equal
    assert changed.field_differences == {"price": 1}


def test_added_removed_changed_are_deterministic_and_samples_are_redacted(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private" / "access_token=filesystem-secret"
    private.mkdir(parents=True)
    legacy = write(
        private / "legacy.ndjson",
        [
            record("https://shop.test/item?id=removed-secret"),
            record("same", api_token="legacy-secret", product_url="https://shop.test/p?token=left"),
        ],
    )
    connector = write(
        private / "connector.ndjson",
        [
            record("https://shop.test/new?id=added-secret"),
            record("same", api_token="connector-secret", product_url="https://shop.test/p?token=right"),
        ],
    )

    report = compare_artifacts(legacy, connector)
    serialized = json.dumps(report.as_dict(), sort_keys=True)

    assert not report.equal
    assert (report.added_count, report.removed_count, report.changed_count) == (1, 1, 1)
    assert report.added_ids == ("https://shop.test/new",)
    assert report.removed_ids == ("https://shop.test/item",)
    assert report.changed_ids == ("same",)
    assert report.samples[0]["fields"]["api_token"] == {
        "legacy": "[redacted]",
        "connector": "[redacted]",
    }
    for secret in (
        "filesystem-secret",
        "legacy-secret",
        "connector-secret",
        "removed-secret",
        "added-secret",
        "token=left",
        "token=right",
        str(tmp_path),
    ):
        assert secret not in serialized


def test_bounds_reject_oversized_lines_and_record_counts(tmp_path: Path) -> None:
    legacy = write(tmp_path / "legacy.ndjson", [record("one"), record("two")])
    connector = write(tmp_path / "connector.ndjson", [])
    with pytest.raises(ArtifactComparisonError, match="exceeds 1 records"):
        compare_artifacts(
            legacy,
            connector,
            limits=ComparisonLimits(max_records_per_artifact=1, max_line_bytes=10_000),
        )
    with pytest.raises(ArtifactComparisonError, match="exceeds 10 bytes"):
        compare_artifacts(
            legacy,
            connector,
            limits=ComparisonLimits(max_records_per_artifact=10, max_line_bytes=10),
        )


def test_cli_exit_codes_and_machine_readable_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy = write(tmp_path / "legacy.ndjson", [record("one")])
    equal = write(tmp_path / "equal.ndjson", [record("one")])
    changed = write(tmp_path / "changed.ndjson", [record("one", price=99)])

    monkeypatch.setattr(sys, "argv", ["catalogue-shadow-compare", str(legacy), str(equal)])
    assert shadow_compare.main() == 0
    assert json.loads(capsys.readouterr().out)["equal"] is True

    monkeypatch.setattr(sys, "argv", ["catalogue-shadow-compare", str(legacy), str(changed)])
    assert shadow_compare.main() == 1
    assert json.loads(capsys.readouterr().out)["equal"] is False

    missing = tmp_path / "missing.ndjson"
    monkeypatch.setattr(sys, "argv", ["catalogue-shadow-compare", str(legacy), str(missing)])
    assert shadow_compare.main() == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert str(tmp_path) not in output.err


def test_cli_loads_only_versioned_reviewed_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy = write(tmp_path / "legacy.ndjson", [record("one", fetched_at="old")])
    connector = write(tmp_path / "connector.ndjson", [record("one", fetched_at="new")])
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"version": 1, "ignore_fields": ["fetched_at"]}))
    monkeypatch.setattr(
        sys,
        "argv",
        ["catalogue-shadow-compare", str(legacy), str(connector), "--rules", str(rules)],
    )

    assert shadow_compare.main() == 0
    assert json.loads(capsys.readouterr().out)["rules"]["ignore_fields"] == ["fetched_at"]

    rules.write_text(json.dumps({"version": 2, "ignore_fields": ["fetched_at"]}))
    assert shadow_compare.main() == 2
    assert "rules.version must be 1" in capsys.readouterr().err
