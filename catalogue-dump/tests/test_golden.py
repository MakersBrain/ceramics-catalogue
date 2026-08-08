"""Characterization tests: every cached source must keep producing its dump.

These exist for the refactor. The claim phase 1 makes is that moving the
orchestrator around changes no output, and this is the only thing that turns
that claim into something a build can check. They are deliberately blunt — a
digest over the whole NDJSON — because the interesting failure is "something
changed and nobody meant it to", not any particular field.

They are excluded from the default run (see `addopts` in pyproject.toml) because
replaying every source reads ten thousand cache entries. Run them with:

    pytest -m golden
    pytest -m golden --update-golden      # after an intended change, reviewed in the diff
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest

from . import golden_support as support

CACHED = support.cached_sources()


def pytest_generate_tests(metafunc):  # pragma: no cover - collection hook
    if "source" in metafunc.fixturenames:
        metafunc.parametrize("source", CACHED, ids=CACHED)


@pytest.mark.golden
@pytest.mark.skipif(not CACHED, reason="no response cache checked out; nothing to replay")
def test_source_replays_to_its_frozen_dump(source, request):
    update = request.config.getoption("--update-golden")
    payload = asyncio.run(support.collect(source))
    actual = support.freeze(source, payload)

    path = support.golden_path(source)
    if update or not path.exists():
        support.write_golden(source, actual)
        if not update:
            pytest.skip(f"wrote a new golden file for {source}; commit it and rerun")
        return

    expected = json.loads(path.read_text(encoding="utf-8"))

    # Counts first: a digest mismatch alone says "something moved", and the
    # record count is usually what actually says what.
    assert actual["records"] == expected["records"], (
        f"{source}: {actual['records']} records, expected {expected['records']}"
    )
    assert actual["extraction_method"] == expected["extraction_method"]
    assert actual["field_coverage"] == expected["field_coverage"]

    if actual["digest"] != expected["digest"]:
        # The digest cannot be diffed, so leave the full output somewhere a
        # person can look at it rather than only reporting two hex strings.
        dump_dir = Path(os.environ.get("CATALOGUE_GOLDEN_DIFF_DIR") or tempfile.mkdtemp(prefix="golden-"))
        written = dump_dir / f"{source}.ndjson"
        written.write_text(support.serialise(payload["records"]), encoding="utf-8")
        assert actual["sample"] == expected["sample"], (
            f"{source}: output changed; full replay written to {written}"
        )
        pytest.fail(
            f"{source}: the sampled records match but the full dump differs "
            f"(digest {actual['digest'][:16]} != {expected['digest'][:16]}); "
            f"full replay written to {written}"
        )


@pytest.mark.golden
def test_the_cache_covers_most_platform_scrapers():
    """A golden suite that only covers three platforms proves little.

    Asserted as a floor rather than a list so adding a scraper is not a test
    failure, but silently losing the cache for half of them is.
    """
    if not CACHED:
        pytest.skip("no response cache checked out")
    sources = support.sources()
    covered = {sources[name].scraper for name in CACHED}
    assert len(covered) >= 8, f"only {len(covered)} scrapers are replayable: {sorted(covered)}"
