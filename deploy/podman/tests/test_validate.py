from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


render = module("render")
validate = module("validate")


def test_validate_accepts_rendered_bundle(tmp_path: Path) -> None:
    render.render(ROOT / "values.example.json", tmp_path)
    validate.validate(tmp_path)


def test_validate_rejects_admin_credential_in_runtime(tmp_path: Path) -> None:
    render.render(ROOT / "values.example.json", tmp_path)
    control = tmp_path / "catalogue-control.container"
    control.write_text(
        control.read_text(encoding="utf-8")
        + "\nVolume=/x/nats-admin-credentials.json:/run/admin.json:ro\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"admin credential|administrative capability"):
        validate.validate(tmp_path)
