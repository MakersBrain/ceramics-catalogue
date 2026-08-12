"""The rules that decide what a load may retire.

These are the loader's safety property, and phase 3 moves the loader in-process.
The plan is explicit that they "deserve tests before the move, not after": every
one of them exists because getting it wrong marks a live catalogue withdrawn,
and that is not visible until someone notices a supplier mysteriously stopped
stocking anything.

`Load.whole` is the single bit that carries all of it. True means "this file is
the entirety of that supplier's catalogue, so anything of theirs it does not
list has been withdrawn". Every test below is about a way that can be false.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mb_ceramics_catalogue.storage import postgres as load_postgres


def write_dump(directory: Path, source: str, count: int, partial: bool = False) -> Path:
    suffix = ".partial.ndjson" if partial else ".ndjson"
    path = directory / f"{source}{suffix}"
    path.write_text(
        "".join(
            json.dumps({"external_id": f"{source}:{index}", "name": f"row {index}"}) + "\n"
            for index in range(count)
        ),
        encoding="utf-8",
    )
    return path


def write_manifest(directory: Path, sources: dict[str, dict]) -> None:
    (directory / "manifest.json").write_text(json.dumps({"sources": sources}), encoding="utf-8")


def plan_for(directory: Path) -> dict[str, load_postgres.Load]:
    plans, _ = load_postgres.plan_load(directory)
    return {plan.source: plan for plan in plans}


def reasons_for(directory: Path) -> dict[str, list[str]]:
    _, skipped = load_postgres.plan_load(directory)
    found: dict[str, list[str]] = {}
    for source, why in skipped:
        found.setdefault(source, []).append(why)
    return found


@pytest.fixture
def dump(tmp_path: Path) -> Path:
    return tmp_path


def test_a_complete_dump_with_no_manifest_is_taken_at_face_value(dump: Path):
    """Older dump directories predate the manifest; they keep the old behaviour."""
    write_dump(dump, "ceradel", 3)
    plan = plan_for(dump)["ceradel"]
    assert plan.whole is True
    assert plan.records == 3


def test_an_empty_dump_is_never_grounds_for_retirement(dump: Path):
    """A scrape that collected nothing says nothing about what the shop sells.

    Loading it and retiring against it would withdraw that supplier's entire
    catalogue, which is the single most expensive mistake this module can make.
    """
    write_dump(dump, "ceradel", 0)
    assert "ceradel" not in plan_for(dump)
    assert reasons_for(dump)["ceradel"] == ["no records in this dump"]


def test_a_partial_alone_is_loaded_but_only_ever_adds(dump: Path):
    write_dump(dump, "ceradel", 5, partial=True)
    plan = plan_for(dump)["ceradel"]
    assert plan.whole is False
    assert plan.records == 5
    assert plan.path.name == "ceradel.partial.ndjson"


def test_a_complete_dump_supersedes_a_partial_one(dump: Path):
    """Replaying the leftovers of an interrupted attempt would re-activate rows
    the complete dump had just retired."""
    write_dump(dump, "ceradel", 9)
    write_dump(dump, "ceradel", 4, partial=True)
    plan = plan_for(dump)["ceradel"]
    assert plan.path.name == "ceradel.ndjson"
    assert plan.records == 9
    assert any("superseded by a complete dump" in why for why in reasons_for(dump)["ceradel"])


def test_a_preserved_older_file_is_the_dangerous_one(dump: Path):
    """`preserved_existing_nonempty` means this run wrote nothing.

    The file on disk looks complete because it *is* complete — for some earlier
    run. Retiring against it withdraws whatever the supplier has listed since.
    """
    write_dump(dump, "ceradel", 7)
    write_manifest(dump, {"ceradel": {"write_status": "preserved_existing_nonempty"}})
    plan = plan_for(dump)["ceradel"]
    assert plan.whole is False
    assert any("not written by this run" in why for why in reasons_for(dump)["ceradel"])


def test_a_truncated_run_is_adds_only(dump: Path):
    """`--limit` caps a source, so the file is a sample, not a catalogue."""
    write_dump(dump, "ceradel", 40)
    write_manifest(dump, {"ceradel": {"write_status": "replaced", "truncated": True}})
    plan = plan_for(dump)["ceradel"]
    assert plan.whole is False
    assert any("hit its cap" in why for why in reasons_for(dump)["ceradel"])


def test_a_source_missing_from_the_manifest_is_adds_only(dump: Path):
    """A file the manifest does not account for was not written by this run."""
    write_dump(dump, "ceradel", 3)
    write_manifest(dump, {"mayco": {"write_status": "replaced"}})
    plan = plan_for(dump)["ceradel"]
    assert plan.whole is False
    assert any("no manifest entry" in why for why in reasons_for(dump)["ceradel"])


def test_a_replaced_untruncated_run_is_authoritative(dump: Path):
    write_dump(dump, "ceradel", 12)
    write_manifest(dump, {"ceradel": {"write_status": "replaced", "truncated": False}})
    assert plan_for(dump)["ceradel"].whole is True


def test_blank_lines_are_not_records(dump: Path):
    """`records_in` counts rows, and an empty file with a newline is still empty."""
    (dump / "ceradel.ndjson").write_text("\n\n  \n", encoding="utf-8")
    assert load_postgres.records_in(dump / "ceradel.ndjson") == 0
    assert "ceradel" not in plan_for(dump)


def test_nothing_is_ever_dropped_silently(dump: Path):
    """Every source in the directory is either planned or explained.

    A source left out of a load with no reason printed is the failure this
    module's `skipped` list exists to prevent.
    """
    write_dump(dump, "ceradel", 5)
    write_dump(dump, "mayco", 0)
    write_dump(dump, "spectrum", 2, partial=True)
    write_dump(dump, "amaco", 8)
    write_dump(dump, "amaco", 3, partial=True)
    write_manifest(dump, {"ceradel": {"write_status": "replaced"}})

    plans, skipped = load_postgres.plan_load(dump)
    accounted = {plan.source for plan in plans} | {source for source, _ in skipped}
    assert accounted == {"ceradel", "mayco", "spectrum", "amaco"}
    # amaco is planned *and* explained: its partial was set aside.
    assert "amaco" in {plan.source for plan in plans}
    assert "amaco" in {source for source, _ in skipped}


def test_an_empty_directory_plans_nothing(dump: Path):
    plans, skipped = load_postgres.plan_load(dump)
    assert plans == []
    assert skipped == []
