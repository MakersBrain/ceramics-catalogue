from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("catalogue_runtime_stage", ROOT / "build_runtime_stage.py")
assert SPEC and SPEC.loader
runtime_stage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime_stage)


def secret_export(root: Path) -> Path:
    for name in (
        "CATALOGUE_SERVICE_DB_PASSWORD",
        "CATALOGUE_CONTROL_DB_PASSWORD",
        "CATALOGUE_DISPATCHER_DB_PASSWORD",
        "CATALOGUE_WORKER_DB_PASSWORD",
    ):
        path = root / "catalogue/database" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("d" * 40)
    token = root / "catalogue/application/CATALOGUE_CONTROL_TOKEN"
    token.parent.mkdir(parents=True)
    token.write_text("t" * 40)
    for role in ("PUBLISH", "CONSUME", "STATS", "ADMIN"):
        path = root / "catalogue/queue" / f"NATS_{role}_PASSWORD"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(role.lower() + "x" * 40)
    return root


def test_builds_scoped_environments_and_credentials(tmp_path: Path) -> None:
    output = tmp_path / "output"
    runtime_stage.build(ROOT / "values.example.json", secret_export(tmp_path / "input"), output)
    service = (output / "config/service.env").read_text()
    worker = (output / "config/worker.env").read_text()
    assert "catalogue_service" in service
    assert "sslmode=verify-full" in service
    assert "CATALOGUE_DUMPS_DIR=/var/lib/catalogue/dumps" in worker
    assert "CATALOGUE_CONTROL_TOKEN=" not in worker
    assert "CATALOGUE_CONTROL_TOKEN=" in (output / "config/explorer.env").read_text()
    assert (output / "secrets/nats-server.conf").stat().st_mode & 0o777 == 0o400
    stats = json.loads((output / "secrets/nats-stats-credentials.json").read_text())
    assert stats["user"] == "catalogue-stats"


def test_rejects_missing_scoped_secret(tmp_path: Path) -> None:
    root = secret_export(tmp_path / "input")
    (root / "catalogue/queue/NATS_ADMIN_PASSWORD").unlink()
    with pytest.raises(ValueError, match="NATS_ADMIN_PASSWORD"):
        runtime_stage.build(ROOT / "values.example.json", root, tmp_path / "output")
