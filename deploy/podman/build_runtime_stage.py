#!/usr/bin/env python3
"""Build exact process environments and NATS credentials from a scoped export."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
PASSWORD = TOKEN
DB_ROLES = {
    "service": ("catalogue_service", "CATALOGUE_SERVICE_DB_PASSWORD"),
    "control": ("catalogue_control", "CATALOGUE_CONTROL_DB_PASSWORD"),
    "dispatcher": ("catalogue_dispatcher", "CATALOGUE_DISPATCHER_DB_PASSWORD"),
    "worker": ("catalogue_worker", "CATALOGUE_WORKER_DB_PASSWORD"),
    "worker-browser": ("catalogue_worker", "CATALOGUE_WORKER_DB_PASSWORD"),
}
NATS_ROLES = {
    "publish": "catalogue-publisher",
    "consume": "catalogue-consumer",
    "stats": "catalogue-stats",
    "admin": "catalogue-admin",
}


def _load_nats_renderer():
    spec = importlib.util.spec_from_file_location("catalogue_nats_renderer", HERE / "render_nats_config.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _secret(root: Path, relative: str, pattern: re.Pattern[str]) -> str:
    path = root / relative
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"required secret input is missing: {relative}")
    value = path.read_text(encoding="utf-8")
    if not pattern.fullmatch(value):
        raise ValueError(f"secret input is invalid: {relative}")
    return value


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def build(values_path: Path, secret_root: Path, output: Path) -> None:
    if output.exists() and (output.is_symlink() or not output.is_dir() or any(output.iterdir())):
        raise ValueError("runtime stage must be absent or an empty directory")
    values = json.loads(values_path.read_text(encoding="utf-8"))
    # Reuse the release renderer's strict public-values validation.
    render_spec = importlib.util.spec_from_file_location("catalogue_render", HERE / "render.py")
    assert render_spec and render_spec.loader
    render = importlib.util.module_from_spec(render_spec)
    render_spec.loader.exec_module(render)
    render.load_values(values_path)

    config = output / "config"
    secrets = output / "secrets"
    config.mkdir(parents=True, exist_ok=True, mode=0o700)
    secrets.mkdir(parents=True, exist_ok=True, mode=0o700)
    host = values["postgres_host"]
    port = values["postgres_port"]
    database = values["postgres_database"]
    common = "CATALOGUE_LOG_JSON=true\n"
    for process, (role, password_name) in DB_ROLES.items():
        password = _secret(secret_root, f"catalogue/database/{password_name}", TOKEN)
        dsn = (
            f"postgresql://{role}:{quote(password, safe='')}@{host}:{port}/{database}"
            "?sslmode=verify-full&sslrootcert=/run/database/postgres-ca.crt"
        )
        additions = "CATALOGUE_DSN=" + dsn + "\n"
        if process in {"worker", "worker-browser"}:
            additions += "CATALOGUE_CACHE_DIR=/var/lib/catalogue/cache\nCATALOGUE_DUMPS_DIR=/var/lib/catalogue/dumps\n"
        _write(config / f"{process}.env", common + additions)

    control_token = _secret(secret_root, "catalogue/application/CATALOGUE_CONTROL_TOKEN", TOKEN)
    for process in ("control", "dispatcher"):
        path = config / f"{process}.env"
        _write(path, path.read_text(encoding="utf-8") + f"CATALOGUE_CONTROL_TOKEN={control_token}\n")
    _write(
        config / "explorer.env",
        f"CATALOGUE_CONTROL_TOKEN={control_token}\nHOST=0.0.0.0\nPORT=3000\n",
    )

    for role, user in NATS_ROLES.items():
        password = _secret(
            secret_root,
            f"catalogue/queue/NATS_{role.upper()}_PASSWORD",
            PASSWORD,
        )
        _write(
            secrets / f"nats-{role}-credentials.json",
            json.dumps({"user": user, "password": password}) + "\n",
        )
    _load_nats_renderer().render(secrets, secrets / "nats-server.conf")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--values", type=Path, required=True)
    parser.add_argument("--secret-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.values, args.secret_root, args.output)


if __name__ == "__main__":
    main()
