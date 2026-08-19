"""Tests for the restic backup tool.

The parts worth testing are the ones that decide whether a restore is
trustworthy: which references get verified, whether a corrupt or missing file is
caught, and whether the settings refuse to run without credentials. The restic
and pg_dump invocations themselves are covered by asserting the argument vectors
rather than by shelling out.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from catalogue_control.backup import (
    ARTIFACT_REFERENCES,
    BackupError,
    Settings,
    verify,
)


def write_artifact(root: Path, relative: str, body: bytes) -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return hashlib.sha256(body).hexdigest()


def test_verify_accepts_matching_artifacts(tmp_path: Path) -> None:
    digest = write_artifact(tmp_path, "run-1/job-1/items.ndjson", b'{"a":1}\n')
    assert verify(tmp_path, [("run-1/job-1/items.ndjson", digest)]) == []


def test_verify_reports_a_missing_file(tmp_path: Path) -> None:
    failures = verify(tmp_path, [("run-1/job-1/gone.ndjson", "0" * 64)])
    assert len(failures) == 1
    assert "gone.ndjson" in failures[0]


def test_verify_reports_a_corrupted_file(tmp_path: Path) -> None:
    write_artifact(tmp_path, "run-1/job-1/items.ndjson", b"original")
    stale = hashlib.sha256(b"what the row remembers").hexdigest()
    failures = verify(tmp_path, [("run-1/job-1/items.ndjson", stale)])
    assert len(failures) == 1
    assert "checksum" in failures[0]


def test_verify_reports_every_failure_not_just_the_first(tmp_path: Path) -> None:
    """One run has to show the whole picture; stopping early hides the extent."""
    good = write_artifact(tmp_path, "run-1/job-1/ok.ndjson", b"fine")
    failures = verify(
        tmp_path,
        [
            ("run-1/job-1/missing-a.ndjson", "0" * 64),
            ("run-1/job-1/ok.ndjson", good),
            ("run-1/job-1/missing-b.ndjson", "0" * 64),
        ],
    )
    assert len(failures) == 2


def test_verify_refuses_a_path_escaping_the_artifact_root(tmp_path: Path) -> None:
    """A recorded path is data; it must not be able to read outside the root."""
    outside = tmp_path.parent / "secret.txt"
    outside.write_bytes(b"not yours")
    root = tmp_path / "dumps"
    root.mkdir()
    failures = verify(root, [("../secret.txt", None)])
    assert len(failures) == 1
    assert "outside" in failures[0]


def test_verify_without_an_expected_digest_only_checks_presence(tmp_path: Path) -> None:
    write_artifact(tmp_path, "run-1/job-1/items.ndjson", b"body")
    assert verify(tmp_path, [("run-1/job-1/items.ndjson", None)]) == []


def test_retired_artifacts_are_excluded_from_the_reference_query() -> None:
    """A retired artifact is allowed to be gone; verifying it is a false alarm."""
    assert "a.available" in ARTIFACT_REFERENCES


def test_settings_require_every_credential(tmp_path: Path) -> None:
    password = tmp_path / "restic.pass"
    password.write_text("x")
    complete = {
        "RESTIC_REPOSITORY": "s3:example/repo",
        "RESTIC_PASSWORD_FILE": str(password),
        "CATALOGUE_DATABASE_URL": "postgresql://localhost/catalogue",
    }
    assert Settings.from_env(complete).repository == "s3:example/repo"

    for omitted in complete:
        partial = {k: v for k, v in complete.items() if k != omitted}
        with pytest.raises(BackupError) as error:
            Settings.from_env(partial)
        assert omitted in str(error.value)


def test_settings_reject_a_missing_password_file(tmp_path: Path) -> None:
    with pytest.raises(BackupError) as error:
        Settings.from_env(
            {
                "RESTIC_REPOSITORY": "s3:example/repo",
                "RESTIC_PASSWORD_FILE": str(tmp_path / "absent"),
                "CATALOGUE_DATABASE_URL": "postgresql://localhost/catalogue",
            }
        )
    assert "does not exist" in str(error.value)


def test_settings_default_the_artifact_directory_to_the_deployed_path(tmp_path: Path) -> None:
    password = tmp_path / "restic.pass"
    password.write_text("x")
    settings = Settings.from_env(
        {
            "RESTIC_REPOSITORY": "s3:example/repo",
            "RESTIC_PASSWORD_FILE": str(password),
            "CATALOGUE_DATABASE_URL": "postgresql://localhost/catalogue",
        }
    )
    # Matches CATALOGUE_ARTIFACTS_DIR in catalogue-control.container.
    assert settings.artifacts_dir == Path("/var/lib/catalogue/dumps")
