# Catalogue backup and restore

Status: implemented, not yet rehearsed against production storage.
Tool: `catalogue-backup` (`catalogue_control.backup`), image `docker/backup`.

## What is protected, and what deliberately is not

| Data | Backed up | Why |
| --- | --- | --- |
| PostgreSQL `catalogue` schema | yes | Runs, jobs, datasets and every artifact reference. |
| `catalogue-dumps` volume | yes | The NDJSON artifacts those references describe. |
| `catalogue-cache` volume | **no** | Reproducible by fetching. Losing it costs one slow run, not data. |
| `catalogue-nats` volume | **no** | Queue state, not a record. See "NATS" below. |

The audit trail is the **pair**: a row records `artifact_path` and
`artifact_sha256`, and the file is what those describe. A database without its
artifacts is a set of dangling references; artifacts without the database are
anonymous files in run/job directories. Neither half alone is a backup.

## Why the database is dumped before the artifacts

`catalogue-backup backup` runs two passes, database first, and the order is the
correctness argument rather than a preference.

Artifacts are **write-once**: a job writes `<run-id>/<job-id>/…` and never
rewrites it. So every row in the database dump refers to a file that already
existed when the dump was taken and cannot change afterwards. Files created
between the two passes are simply not referenced by the dump — harmless extra
data in the snapshot.

Reversing the order breaks exactly this: a job finishing between the file pass
and the dump lands a row whose artifact was never captured, producing a restore
with dangling references. That is the one failure this design exists to prevent.

## Configuration

Everything comes from the environment. No credential is accepted on the command
line, where it would land in shell history and the process table.

| Variable | Meaning |
| --- | --- |
| `RESTIC_REPOSITORY` | e.g. `s3:https://s3.fr-par.scw.cloud/makersbrain-<env>-backups/collector` |
| `RESTIC_PASSWORD_FILE` | Path to the repository password. A file, never an inline value. |
| `CATALOGUE_DATABASE_URL` | PostgreSQL connection URL. |
| `CATALOGUE_ARTIFACTS_DIR` | Defaults to `/var/lib/catalogue/dumps`. |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | Scaleway object storage credentials. |

The repository password is the encryption key. **If it is lost, the backups are
unreadable** — restic has no recovery path. It belongs in the secret manager
alongside the object storage credentials, and it must be escrowed separately
from the bucket it protects.

## Routine backup

```
catalogue-backup backup
```

Initialises the restic repository on first use, dumps the database with
`pg_dump --format=custom`, backs it up tagged `database`, then backs up the
artifact directory tagged `artifacts`. Both passes share an `at:<timestamp>` tag
so the halves of one run can be found together.

Retention:

```
catalogue-backup forget --prune --keep-daily 7 --keep-weekly 5 --keep-monthly 12
```

## Restore

```
catalogue-backup restore --snapshot latest --target /var/lib/catalogue-restore
pg_restore --clean --if-exists --no-owner --no-privileges \
  --dbname "$CATALOGUE_DATABASE_URL" /var/lib/catalogue-restore/…/catalogue.dump
CATALOGUE_ARTIFACTS_DIR=/var/lib/catalogue-restore/…/dumps catalogue-backup verify
```

**A restore is not finished until `verify` passes.** It re-reads every artifact
reference in the restored database and checks the file resolves within the
artifact root and its sha256 matches, reusing `changes.resolve_artifact` so the
integrity rule lives in exactly one place. It reports every broken reference in
one run rather than stopping at the first, and exits non-zero if any fail.

Artifacts marked `available = false` are excluded: that flag means the catalogue
has retired the artifact and the file is allowed to be gone. Verifying them
would report a deliberate retention decision as a corrupt restore, and a check
that cries wolf is a check that gets ignored.

## NATS

`catalogue-nats` holds queue state, not the record of what happened — that is in
PostgreSQL. A restore brings back a catalogue whose in-flight jobs are lost;
they are re-dispatched from their database rows. Backing up JetStream state
would restore a queue that disagrees with the restored database, which is worse
than an empty one.

## What still has to happen before this counts as a backup

1. **Rehearse a restore into a scratch database and prove `verify` passes.**
   Until that is done this is an untested script, not a recovery capability.
2. Create the `collector` prefix and its credentials in `mb-infra`
   (`modules/scaleway-backups` already provides the bucket, versioning,
   append-only writer policy and production object lock).
3. Add the Quadlet unit and systemd timer in `mb-infra`. Scheduling,
   credentials and storage are infrastructure-owned per the repository split
   plan section 10.6; this repository owns the image and the data semantics.
4. Alert on backup age. A silent failure is indistinguishable from success
   until the day it matters — `catalogue-backup snapshots` is the signal.
5. Escrow the restic password separately from the bucket credentials.
