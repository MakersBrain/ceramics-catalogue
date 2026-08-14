#!/usr/bin/env python3
"""Explicitly invoke the control plane's fixed-target bounded paid probe."""

from __future__ import annotations

import argparse
import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-paid-probe", action="store_true")
    parser.add_argument("--route-id", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--control-url", default="http://127.0.0.1:8687")
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--key-id", default="current")
    options = parser.parse_args()
    if not options.allow_paid_probe:
        raise SystemExit("refusing paid traffic without --allow-paid-probe")
    token = os.environ.get("CATALOGUE_CONTROL_TOKEN", "")
    if not token:
        raise SystemExit("CATALOGUE_CONTROL_TOKEN is required")
    path = f"/v1/proxy/routes/{options.route_id}/probe"
    now = int(time.time())
    claims = {
        "kid": options.key_id, "sub": options.actor, "role": "admin",
        "aud": "catalogue-control", "iat": now, "exp": now + 45,
        "auth_time": now, "nonce": str(uuid4()), "method": "POST", "path": path,
    }
    raw = json.dumps(claims, separators=(",", ":")).encode()
    key = serialization.load_pem_private_key(options.private_key.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("operator assertion key must be Ed25519")
    request = urllib.request.Request(
        options.control_url.rstrip("/") + path,
        data=json.dumps({"confirmation": "SPEND UP TO 1.1 MB"}).encode(), method="POST",
        headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json",
            "Idempotency-Key": str(uuid4()), "X-Catalogue-Actor": encoded(raw),
            "X-Catalogue-Actor-Signature": encoded(key.sign(raw)),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise SystemExit(f"paid probe rejected with HTTP {error.code}") from None
    print(json.dumps({
        "state": result.get("state"), "application_bytes": result.get("application_bytes"),
        "reserved_bytes": result.get("reserved_bytes"), "latency_ms": result.get("latency_ms"),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
