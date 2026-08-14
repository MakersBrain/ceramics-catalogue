#!/usr/bin/env python3
"""Generate the explorer signing key and control verification-key set."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, default=Path("deploy/secrets"))
    parser.add_argument("--key-id", default="current")
    options = parser.parse_args()
    private_path = options.directory / "operator-private-key.pem"
    public_path = options.directory / "operator-public-keys.json"
    if private_path.exists() or public_path.exists():
        raise SystemExit("refusing to overwrite an operator key; rotate with a new key id")
    options.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(options.directory, 0o700)
    private = Ed25519PrivateKey.generate()
    private_path.write_bytes(private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    os.chmod(private_path, 0o600)
    public = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    public_path.write_text(json.dumps({options.key_id: public}, indent=2) + "\n")
    os.chmod(public_path, 0o644)
    print(f"wrote {private_path} and {public_path}")


if __name__ == "__main__":
    main()
