#!/usr/bin/env python3
"""Render a private NATS permission config from four role credential files."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

PASSWORD = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
ROLE_USERS = {
    "publish": "catalogue-publisher",
    "consume": "catalogue-consumer",
    "stats": "catalogue-stats",
    "admin": "catalogue-admin",
}


def credential(path: Path, role: str) -> tuple[str, str]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if set(document) != {"user", "password"}:
        raise ValueError(f"{role} credentials must contain only user and password")
    user = document["user"]
    password = document["password"]
    if user != ROLE_USERS[role]:
        raise ValueError(f"{role} credentials use an unexpected user")
    if not isinstance(password, str) or not PASSWORD.fullmatch(password):
        raise ValueError(f"{role} password must be a bounded URL-safe secret")
    return user, password


def render(credentials_root: Path, output: Path) -> None:
    if output.exists() or output.is_symlink():
        raise ValueError("refusing to overwrite NATS configuration")
    values = {
        role: credential(credentials_root / f"nats-{role}-credentials.json", role)
        for role in ROLE_USERS
    }
    users = []
    permissions = {
        "publish": ('["catalogue.jobs.>"]', '["_INBOX.>"]'),
        "consume": (
            '["$JS.API.CONSUMER.INFO.CATALOGUE_JOBS.>", '
            '"$JS.API.CONSUMER.MSG.NEXT.CATALOGUE_JOBS.>", "$JS.ACK.>"]',
            '["_INBOX.>", "catalogue.jobs.>"]',
        ),
        "stats": (
            '["$JS.API.STREAM.INFO.CATALOGUE_JOBS", '
            '"$JS.API.CONSUMER.INFO.CATALOGUE_JOBS.>"]',
            '["_INBOX.>"]',
        ),
        "admin": ('[">"]', '[">"]'),
    }
    for role, (user, password) in values.items():
        publish, subscribe = permissions[role]
        users.append(
            f'    {{user: "{user}", password: "{password}", permissions: '
            f"{{publish: {publish}, subscribe: {subscribe}}}}}"
        )
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.write_text(
        "jetstream { store_dir: /data }\n"
        "http: 8222\n"
        "authorization {\n  users = [\n"
        + ",\n".join(users)
        + "\n  ]\n}\n",
        encoding="utf-8",
    )
    output.chmod(0o400)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credentials-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    render(args.credentials_root, args.output)


if __name__ == "__main__":
    main()
