"""Stage the provider API key, then run the control plane unprivileged."""

from __future__ import annotations

import os
from pathlib import Path
import pwd
import shutil


USER = pwd.getpwnam("catalogue")
PRIVATE_DIR = Path("/run/catalogue-secrets")


def main() -> None:
    source = Path("/run/secrets/decodo.env")
    if source.is_file() and source.stat().st_size:
        PRIVATE_DIR.mkdir(mode=0o700, exist_ok=True)
        os.chown(PRIVATE_DIR, USER.pw_uid, USER.pw_gid)
        destination = PRIVATE_DIR / "decodo.env"
        shutil.copyfile(source, destination)
        os.chown(destination, USER.pw_uid, USER.pw_gid)
        os.chmod(destination, 0o400)
        os.environ["CATALOGUE_PROXY_API_SECRET_FILE"] = str(destination)
    else:
        os.environ.pop("CATALOGUE_PROXY_API_SECRET_FILE", None)

    os.setgroups([])
    os.setgid(USER.pw_gid)
    os.setuid(USER.pw_uid)
    os.execvp("catalogue-control", ["catalogue-control"])


if __name__ == "__main__":
    main()
