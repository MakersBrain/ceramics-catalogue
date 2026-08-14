"""Stage bind-mounted secrets, then run the catalogue worker unprivileged."""

from __future__ import annotations

import os
from pathlib import Path
import pwd
import sys


WORKER = pwd.getpwnam("catalogue")
WORKER_UID = WORKER.pw_uid
WORKER_GID = WORKER.pw_gid
def main() -> None:
    # Control owns provider credentials and atomically replaces this file.
    # Workers read the shared volume at job start, so rotations take effect
    # without restarting processes and no worker ever receives the API key.
    profiles = Path("/run/proxy-secrets/profiles.json")
    if profiles.is_file():
        os.environ["CATALOGUE_PROXY_SECRET_FILE"] = str(profiles)
    else:
        os.environ.pop("CATALOGUE_PROXY_SECRET_FILE", None)
    os.environ.pop("CATALOGUE_PROXY_API_SECRET_FILE", None)

    os.setgroups([])
    os.setgid(WORKER_GID)
    os.setuid(WORKER_UID)
    os.execvp("catalogue-worker", ["catalogue-worker", *sys.argv[1:]])


if __name__ == "__main__":
    main()
