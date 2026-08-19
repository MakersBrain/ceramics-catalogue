#!/usr/bin/env python3
"""Verify, stage and atomically activate a signed Catalogue release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import render
import validate

RELEASE_ID = re.compile(r"^catalogue-[0-9]{4}\.[0-9]{2}\.[0-9]{2}-[a-f0-9]{16,64}$")
COMMIT = re.compile(r"^[a-f0-9]{40,64}$")
QUALIFICATION = re.compile(r"^\S+/qualifications@sha256:[a-f0-9]{64}$")
COSIGN_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
COSIGN_IDENTITY = (
    "https://github.com/MakersBrain/ceramics-catalogue/"
    ".github/workflows/release.yml@refs/heads/main"
)
BASE_UNITS = [
    "catalogue-nats.service",
    "catalogue-service.service",
    "catalogue-control.service",
    "catalogue-explorer.service",
    "catalogue-dispatcher.service",
    "catalogue-worker-browser.service",
]


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def run_best_effort(command: list[str]) -> None:
    subprocess.run(command, check=False)


def verify_and_pull(images: dict[str, str]) -> None:
    for image in images.values():
        run(
            [
                "cosign",
                "verify",
                "--certificate-oidc-issuer",
                COSIGN_OIDC_ISSUER,
                "--certificate-identity",
                COSIGN_IDENTITY,
                "--certificate-github-workflow-repository",
                "MakersBrain/ceramics-catalogue",
                image,
            ]
        )
        run(["podman", "pull", image])


def load_record(path: Path, values: dict) -> dict:
    record = json.loads(path.read_text(encoding="utf-8"))
    if not RELEASE_ID.fullmatch(record.get("release_id", "")):
        raise ValueError("release_id is invalid")
    if not COMMIT.fullmatch(record.get("source_commit", "")):
        raise ValueError("source_commit is invalid")
    if not record.get("ci_run_url", "").startswith("https://"):
        raise ValueError("ci_run_url must be HTTPS")
    if record.get("images") != values["images"]:
        raise ValueError("release image map differs from rendered values")
    if record.get("environment") != values["environment"]:
        raise ValueError("release environment differs from rendered values")
    if record.get("compatible_makersbrain_release") != values["compatible_makersbrain_release"]:
        raise ValueError("MakersBrain compatibility reference differs from rendered values")
    if values["environment"] == "production" and not QUALIFICATION.fullmatch(
        record.get("staging_qualification_ref", "")
    ):
        raise ValueError("production requires an immutable staging qualification")
    return record


def units(values: dict) -> list[str]:
    return BASE_UNITS + [f"catalogue-worker@{item}.service" for item in values["worker_instances"]]


def stage(rendered: Path, release_id: str, state_root: Path) -> Path:
    target = state_root / "releases" / release_id
    if target.exists():
        if not target.is_dir():
            raise ValueError("staged release path is not a directory")
        if content_manifest(target) != content_manifest(rendered):
            raise ValueError("staged release content differs from the signed candidate")
        return target
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copytree(rendered, target)
    return target


def content_manifest(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.is_symlink():
            raise ValueError("release files must not be symlinks")
        result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def activate(target: Path, release_units: list[str], quadlet_root: Path) -> None:
    quadlet_root.mkdir(parents=True, exist_ok=True, mode=0o750)
    current = quadlet_root / "catalogue"
    if current.exists() and not current.is_symlink():
        raise ValueError("Catalogue Quadlet activation path is not a managed symlink")
    previous = os.readlink(current) if current.is_symlink() else None
    temporary = quadlet_root / f".catalogue-{target.name}"
    temporary.unlink(missing_ok=True)
    try:
        os.symlink(target, temporary, target_is_directory=True)
        os.replace(temporary, current)
        run(["systemctl", "--user", "daemon-reload"])
        for unit in release_units:
            run(["systemctl", "--user", "restart", unit])
    except Exception:
        temporary.unlink(missing_ok=True)
        current.unlink(missing_ok=True)
        if previous is not None:
            os.symlink(previous, current, target_is_directory=True)
        run_best_effort(["systemctl", "--user", "daemon-reload"])
        if previous is not None:
            for unit in release_units:
                run_best_effort(["systemctl", "--user", "restart", unit])
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--values", type=Path, required=True)
    parser.add_argument("--release-record", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, default=Path.home() / ".local/state/catalogue")
    parser.add_argument(
        "--quadlet-root", type=Path, default=Path.home() / ".config/containers/systemd"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--activate", action="store_true")
    mode.add_argument("--stage-only", action="store_true")
    mode.add_argument("--start-staged", action="store_true")
    args = parser.parse_args()

    values = render.load_values(args.values)
    record = load_record(args.release_record, values)
    verify_and_pull(values["images"])

    with tempfile.TemporaryDirectory(prefix="catalogue-release-") as temporary:
        rendered = Path(temporary)
        render.render(args.values, rendered)
        validate.validate(rendered)
        target = args.state_root / "releases" / record["release_id"]
        if not args.start_staged:
            target = stage(rendered, record["release_id"], args.state_root)
        elif not target.is_dir():
            raise ValueError("the exact verified release has not been staged")
        if args.activate or args.start_staged:
            activate(target, units(values), args.quadlet_root)
        elif args.stage_only:
            print(f"staged {record['release_id']}")
        else:
            print("Catalogue release images, record and Quadlets are valid")


if __name__ == "__main__":
    main()
