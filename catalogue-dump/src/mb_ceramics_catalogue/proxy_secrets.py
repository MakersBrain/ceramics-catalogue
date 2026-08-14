"""Atomic dynamic proxy credential storage.

Only control writes this file. Workers mount the containing volume read-only and
load a complete generation at job start.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import string
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mb_ceramics_catalogue.proxy import ProxyDenied, load_profiles


def generate_password(length: int = 32) -> str:
    """Meet Decodo's documented password rules without `@` or `:`."""
    if length < 12:
        raise ValueError("Decodo passwords require at least 12 characters")
    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("_~+="),
    ]
    alphabet = string.ascii_letters + string.digits + "_~+="
    chars = required + [secrets.choice(alphabet) for _ in range(length - len(required))]
    # Fisher-Yates through SystemRandom avoids predictable required positions.
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def username_fingerprint(username: str) -> str:
    return hashlib.sha256(username.encode()).hexdigest()


def mask_username(username: str) -> str:
    return f"…{username[-4:]}" if len(username) > 4 else "…"


class ProfileSecretStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with os.fdopen(descriptor, "a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                yield
        finally:
            # fdopen closes the descriptor on both normal and exceptional exit.
            pass

    def read_raw(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        # load_profiles enforces permissions and complete model validity.
        load_profiles(self.path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ProxyDenied("proxy profiles secret must contain an object")
        return raw

    def install(
        self, logical_name: str, *, username: str, password: str,
        host: str = "gate.decodo.com", port: int = 7000,
    ) -> int:
        if not logical_name or any(char not in string.ascii_lowercase + string.digits + "_-" for char in logical_name):
            raise ProxyDenied("invalid logical proxy profile name")
        with self._lock():
            profiles = self.read_raw()
            current = profiles.get(logical_name, {})
            generation = int(current.get("generation", 0)) + 1
            profiles[logical_name] = {
                "host": host,
                "port": port,
                "username": username,
                "password": password,
                "generation": generation,
            }
            self._replace(profiles)
            return generation

    def remove(self, logical_name: str) -> None:
        with self._lock():
            profiles = self.read_raw()
            profiles.pop(logical_name, None)
            self._replace(profiles)

    def _replace(self, profiles: dict[str, dict[str, Any]]) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o400)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(profiles, output, sort_keys=True, separators=(",", ":"))
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o400)
            load_profiles(temporary)
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary.exists():
                temporary.unlink()
