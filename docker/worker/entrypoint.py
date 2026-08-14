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

    # Docker starts this entrypoint as root so it can stage secrets and then
    # drop privileges below.  Environment variables are not updated by
    # setuid(2), however, and leaving HOME=/root makes camoufox look for its
    # downloaded browser and writable config under /root/.cache after the
    # process is already the unprivileged catalogue user.  The browser was
    # fetched into catalogue's home at image-build time, so make the runtime
    # identity internally consistent before execing the worker.
    os.environ["HOME"] = WORKER.pw_dir
    os.environ["USER"] = WORKER.pw_name
    os.environ["LOGNAME"] = WORKER.pw_name

    os.setgroups([])
    os.setgid(WORKER_GID)
    os.setuid(WORKER_UID)
    os.execvp("catalogue-worker", ["catalogue-worker", *sys.argv[1:]])


if __name__ == "__main__":
    main()
