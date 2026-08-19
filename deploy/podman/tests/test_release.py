from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
release = importlib.import_module("release")
render = importlib.import_module("render")


def record() -> dict:
    return json.loads((ROOT / "release-record.example.json").read_text(encoding="utf-8"))


def values() -> dict:
    return render.load_values(ROOT / "values.example.json")


def test_record_binds_image_map_and_makersbrain_release(tmp_path: Path) -> None:
    document = record()
    path = tmp_path / "record.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert release.load_record(path, values())["release_id"].startswith("catalogue-")

    document["compatible_makersbrain_release"] = "control-2026.08.19-deadbeefdeadbeef"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="compatibility"):
        release.load_record(path, values())


def test_production_requires_immutable_qualification(tmp_path: Path) -> None:
    document = record()
    document["environment"] = "production"
    document["staging_qualification_ref"] = ""
    path = tmp_path / "record.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    production = values() | {"environment": "production"}
    with pytest.raises(ValueError, match="qualification"):
        release.load_record(path, production)


def test_record_binds_environment(tmp_path: Path) -> None:
    document = record()
    document["environment"] = "production"
    path = tmp_path / "record.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="environment"):
        release.load_record(path, values())


def test_images_require_the_exact_keyless_identity(monkeypatch) -> None:
    image = "ghcr.io/makersbrain/catalogue/control@sha256:" + "a" * 64
    calls: list[list[str]] = []
    monkeypatch.setattr(release, "run", calls.append)

    release.verify_and_pull({"control": image})

    assert calls == [
        [
            "cosign",
            "verify",
            "--certificate-oidc-issuer",
            release.COSIGN_OIDC_ISSUER,
            "--certificate-identity",
            release.COSIGN_IDENTITY,
            "--certificate-github-workflow-repository",
            "MakersBrain/ceramics-catalogue",
            image,
        ],
        ["podman", "pull", image],
    ]


def test_activation_restores_previous_symlink_on_failure(tmp_path: Path, monkeypatch) -> None:
    previous = tmp_path / "previous"
    candidate = tmp_path / "candidate"
    previous.mkdir()
    candidate.mkdir()
    quadlets = tmp_path / "quadlets"
    quadlets.mkdir()
    (quadlets / "catalogue").symlink_to(previous, target_is_directory=True)
    calls = 0

    def failing_run(command: list[str]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("start failed")

    monkeypatch.setattr(release, "run", failing_run)
    monkeypatch.setattr(release, "run_best_effort", lambda command: None)
    with pytest.raises(RuntimeError, match="start failed"):
        release.activate(candidate, ["catalogue-nats.service"], quadlets)
    assert (quadlets / "catalogue").resolve() == previous.resolve()


def test_stage_rejects_changed_existing_release(tmp_path: Path) -> None:
    rendered = tmp_path / "rendered"
    rendered.mkdir()
    (rendered / "unit.container").write_text("signed", encoding="utf-8")
    target = tmp_path / "state" / "releases" / "catalogue-2026.08.19-0123456789abcdef"
    target.mkdir(parents=True)
    (target / "unit.container").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="differs"):
        release.stage(rendered, target.name, tmp_path / "state")
