"""Back up and restore the catalogue with restic.

    catalogue-backup backup
    catalogue-backup restore --snapshot latest --target /var/lib/catalogue
    catalogue-backup verify  --artifacts /var/lib/catalogue/dumps
    catalogue-backup snapshots
    catalogue-backup forget --prune

Two things have to survive together, and neither is useful alone:

* the PostgreSQL rows, which record for every job an `artifact_path` and an
  `artifact_sha256`; and
* the NDJSON artifacts under the `catalogue-dumps` volume that those digests
  describe.

The audit trail is the *pair*. A database restored without its artifacts is a
catalogue of dangling references, and artifacts without the database are
anonymous files in run/job directories.

Ordering is the whole correctness argument
------------------------------------------
The database is dumped **first**, then the artifacts are backed up.

That order is not arbitrary. Artifacts are write-once: a job writes
`<run-id>/<job-id>/…` and never rewrites it. So every row present in the
database dump refers to a file that already existed when the dump was taken,
and which cannot have changed by the time the file pass runs. Files created
between the two passes are simply not referenced by the dump yet -- harmless
extra data.

Reversing the order breaks exactly this. Files would be captured first, the
dump taken second, and any job finishing in between would land a row in the
dump whose artifact was never backed up: a restore with dangling references,
which is the one failure this design exists to prevent.

`catalogue-cache` is deliberately not backed up. It is reproducible by
fetching, so losing it costs one slow run rather than any data.

Restore is not complete until it is verified
--------------------------------------------
`restore` finishes by re-reading every artifact reference in the restored
database and checking the file resolves and its sha256 matches, reusing
`changes.resolve_artifact` so the rule lives in one place. A restore that
cannot prove its own integrity exits non-zero.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from .changes import ArtifactError, resolve_artifact

# Every artifact reference the catalogue still expects to be able to read.
# `job_artifacts.location` is the per-dataset publication and
# `jobs.artifact_path` the job's own NDJSON; both are checked because either can
# be the only reference to a file.
#
# `available = false` rows are excluded on purpose. That flag marks an artifact
# the catalogue has retired, and a retired file is allowed to be gone --
# verifying it would report a deliberate retention decision as a corrupt
# restore, which is exactly the kind of false alarm that gets a check ignored.
ARTIFACT_REFERENCES = """
select artifact_path, artifact_sha256 from (
    select j.artifact_path as artifact_path, j.artifact_sha256 as artifact_sha256
      from catalogue.jobs j
     where j.artifact_path is not null
    union
    select a.location as artifact_path, a.sha256 as artifact_sha256
      from catalogue.job_artifacts a
     where a.location is not null
       and a.available
) refs
"""

DUMP_NAME = "catalogue.dump"


class BackupError(RuntimeError):
    """A backup or restore step failed and the operator has to know."""


@dataclasses.dataclass(frozen=True)
class Settings:
    """Everything the tool needs, all of it from the environment.

    No credential is ever accepted on the command line, where it would land in
    shell history and in the process table.
    """

    repository: str
    password_file: Path
    postgres_url: str
    artifacts_dir: Path
    tags: tuple[str, ...]

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        missing = [
            name
            for name in ("RESTIC_REPOSITORY", "RESTIC_PASSWORD_FILE", "CATALOGUE_DATABASE_URL")
            if not env.get(name)
        ]
        if missing:
            raise BackupError(
                "missing required environment: "
                + ", ".join(missing)
                + ". Credentials are taken from the environment only, never from argv."
            )
        password_file = Path(env["RESTIC_PASSWORD_FILE"])
        if not password_file.is_file():
            raise BackupError(f"RESTIC_PASSWORD_FILE does not exist: {password_file}")
        return cls(
            repository=env["RESTIC_REPOSITORY"],
            password_file=password_file,
            postgres_url=env["CATALOGUE_DATABASE_URL"],
            artifacts_dir=Path(env.get("CATALOGUE_ARTIFACTS_DIR", "/var/lib/catalogue/dumps")),
            tags=tuple(t for t in env.get("CATALOGUE_BACKUP_TAGS", "catalogue").split(",") if t),
        )


def run(command: Sequence[str], *, env: dict[str, str] | None = None, stdout=None) -> None:
    """Run a command, failing loudly. Never `shell=True`, never interpolated."""
    try:
        subprocess.run(list(command), check=True, env=env, stdout=stdout)
    except FileNotFoundError as error:
        raise BackupError(f"{command[0]} is not installed: {error}") from error
    except subprocess.CalledProcessError as error:
        raise BackupError(f"{command[0]} failed with exit status {error.returncode}") from error


def restic_env(settings: Settings) -> dict[str, str]:
    env = dict(os.environ)
    env["RESTIC_REPOSITORY"] = settings.repository
    env["RESTIC_PASSWORD_FILE"] = str(settings.password_file)
    return env


def ensure_repository(settings: Settings) -> None:
    """Initialise the restic repository the first time, idempotently."""
    probe = subprocess.run(
        ["restic", "cat", "config"],
        env=restic_env(settings),
        capture_output=True,
    )
    if probe.returncode == 0:
        return
    run(["restic", "init"], env=restic_env(settings))


def dump_database(settings: Settings, destination: Path) -> Path:
    """Take a consistent point-in-time dump of the catalogue database."""
    target = destination / DUMP_NAME
    # --format=custom so restore can use pg_restore's ordering and parallelism.
    # The URL goes in argv here because pg_dump offers no file-based form; it is
    # this process's own child, and the URL is not logged.
    run(
        [
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "--file",
            str(target),
            settings.postgres_url,
        ]
    )
    if not target.is_file() or target.stat().st_size == 0:
        raise BackupError("pg_dump produced no output")
    return target


def backup(settings: Settings) -> None:
    """Dump the database, then the artifacts. See the module docstring."""
    ensure_repository(settings)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    tags: list[str] = []
    for tag in (*settings.tags, f"at:{stamp}"):
        tags += ["--tag", tag]

    with tempfile.TemporaryDirectory(prefix="catalogue-dump-") as workdir:
        dump = dump_database(settings, Path(workdir))
        # Pass one: the database. Taken first so no row can reference an
        # artifact that pass two has not seen.
        run(
            ["restic", "backup", "--tag", "database", *tags, str(dump)],
            env=restic_env(settings),
        )

    if not settings.artifacts_dir.is_dir():
        raise BackupError(f"artifact directory is missing: {settings.artifacts_dir}")
    # Pass two: the write-once artifacts.
    run(
        ["restic", "backup", "--tag", "artifacts", *tags, str(settings.artifacts_dir)],
        env=restic_env(settings),
    )


def verify(artifacts_dir: Path, references: Iterable[tuple[str, str | None]]) -> list[str]:
    """Check every recorded artifact resolves and matches its digest.

    Returns the failures rather than raising, so one run reports every broken
    reference instead of stopping at the first.
    """
    failures: list[str] = []
    for recorded, expected in references:
        try:
            resolve_artifact(artifacts_dir, recorded, expected)
        except ArtifactError as error:
            failures.append(f"{recorded}: {error}")
    return failures


def database_references(postgres_url: str) -> list[tuple[str, str | None]]:
    import psycopg  # imported here so `verify` stays unit-testable without a database

    with psycopg.connect(postgres_url) as connection, connection.cursor() as cursor:
        cursor.execute(ARTIFACT_REFERENCES)
        return [(row[0], row[1]) for row in cursor.fetchall()]


def restore(settings: Settings, snapshot: str, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    run(
        ["restic", "restore", snapshot, "--target", str(target)],
        env=restic_env(settings),
    )
    print(f"restored snapshot {snapshot} to {target}", file=sys.stderr)
    print(
        "The database dump and artifacts are on disk. Load the dump with "
        "pg_restore, point CATALOGUE_ARTIFACTS_DIR at the restored dumps "
        "directory, then run `catalogue-backup verify` -- a restore is not "
        "complete until its references check out.",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="catalogue-backup", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("backup", help="dump the database, then back up the artifacts")
    sub.add_parser("snapshots", help="list snapshots in the repository")

    p_restore = sub.add_parser("restore", help="restore a snapshot to a directory")
    p_restore.add_argument("--snapshot", default="latest")
    p_restore.add_argument("--target", type=Path, required=True)

    p_verify = sub.add_parser("verify", help="check every artifact reference resolves")
    p_verify.add_argument("--artifacts", type=Path)

    p_forget = sub.add_parser("forget", help="apply the retention policy")
    p_forget.add_argument("--prune", action="store_true")
    p_forget.add_argument("--keep-daily", default="7")
    p_forget.add_argument("--keep-weekly", default="5")
    p_forget.add_argument("--keep-monthly", default="12")

    args = parser.parse_args(argv)

    try:
        settings = Settings.from_env()

        if args.command == "backup":
            backup(settings)
        elif args.command == "snapshots":
            run(["restic", "snapshots"], env=restic_env(settings))
        elif args.command == "restore":
            restore(settings, args.snapshot, args.target)
        elif args.command == "verify":
            artifacts = args.artifacts or settings.artifacts_dir
            failures = verify(artifacts, database_references(settings.postgres_url))
            if failures:
                print(
                    f"{len(failures)} artifact reference(s) could not be verified:",
                    file=sys.stderr,
                )
                for failure in failures:
                    print(f"  {failure}", file=sys.stderr)
                return 1
            print("every recorded artifact resolves and matches its digest")
        elif args.command == "forget":
            command = [
                "restic", "forget",
                "--keep-daily", str(args.keep_daily),
                "--keep-weekly", str(args.keep_weekly),
                "--keep-monthly", str(args.keep_monthly),
            ]
            if args.prune:
                command.append("--prune")
            run(command, env=restic_env(settings))
    except BackupError as error:
        print(f"catalogue-backup: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
