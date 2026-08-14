"""Stage bind-mounted secrets, then run the catalogue worker unprivileged."""

from __future__ import annotations

import os
from pathlib import Path
import pwd
import shutil
import sys


WORKER = pwd.getpwnam("catalogue")
WORKER_UID = WORKER.pw_uid
WORKER_GID = WORKER.pw_gid
SECRET_DIR = Path("/run/catalogue-secrets")


def _stage_secret(environment: str, source: Path, filename: str) -> None:
    """Copy a private host bind mount to a worker-readable private file."""
    if not source.is_file() or source.stat().st_size == 0:
        os.environ.pop(environment, None)
        return

    SECRET_DIR.mkdir(mode=0o700, exist_ok=True)
    os.chown(SECRET_DIR, WORKER_UID, WORKER_GID)
    os.chmod(SECRET_DIR, 0o700)
    destination = SECRET_DIR / filename
    shutil.copyfile(source, destination)
    os.chown(destination, WORKER_UID, WORKER_GID)
    os.chmod(destination, 0o400)
    os.environ[environment] = str(destination)


def main() -> None:
    _stage_secret(
        "CATALOGUE_PROXY_API_SECRET_FILE",
        Path("/run/secrets/decodo.env"),
        "decodo.env",
    )
    _stage_secret(
        "CATALOGUE_PROXY_SECRET_FILE",
        Path("/run/secrets/proxy-profiles.json"),
        "proxy-profiles.json",
    )

    os.setgroups([])
    os.setgid(WORKER_GID)
    os.setuid(WORKER_UID)
    os.execvp("catalogue-worker", ["catalogue-worker", *sys.argv[1:]])


if __name__ == "__main__":
    main()
