from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "render_nats_config.py"
SPEC = importlib.util.spec_from_file_location("render_nats_config", MODULE_PATH)
assert SPEC and SPEC.loader
render_nats_config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render_nats_config)


def credentials(root: Path) -> None:
    for role, user in render_nats_config.ROLE_USERS.items():
        (root / f"nats-{role}-credentials.json").write_text(
            json.dumps({"user": user, "password": role[0] * 48}),
            encoding="utf-8",
        )


def test_render_scopes_runtime_roles_and_keeps_admin_separate(tmp_path: Path) -> None:
    credentials(tmp_path)
    output = tmp_path / "private" / "nats.conf"
    render_nats_config.render(tmp_path, output)
    rendered = output.read_text(encoding="utf-8")
    assert 'user: "catalogue-publisher"' in rendered
    assert 'publish: ["catalogue.jobs.>"]' in rendered
    assert 'user: "catalogue-consumer"' in rendered
    assert "$JS.API.CONSUMER.MSG.NEXT.CATALOGUE_JOBS.>" in rendered
    assert 'user: "catalogue-stats"' in rendered
    assert 'user: "catalogue-admin"' in rendered
    assert (output.stat().st_mode & 0o777) == 0o400


def test_render_rejects_unexpected_user(tmp_path: Path) -> None:
    credentials(tmp_path)
    (tmp_path / "nats-publish-credentials.json").write_text(
        json.dumps({"user": "catalogue-admin", "password": "p" * 48}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unexpected user"):
        render_nats_config.render(tmp_path, tmp_path / "nats.conf")


def test_render_refuses_overwrite(tmp_path: Path) -> None:
    credentials(tmp_path)
    output = tmp_path / "nats.conf"
    output.write_text("existing", encoding="utf-8")
    with pytest.raises(ValueError, match="overwrite"):
        render_nats_config.render(tmp_path, output)
