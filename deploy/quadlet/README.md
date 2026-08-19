# Quadlet units for the catalogue

Systemd units following the immutable-image approach `makersbrain-infra`
documents in commit `934a7e2`: images are built and tagged per release, and
nothing is built on the host.

These live here rather than in `makersbrain-infra` on purpose. They belong to
this project — the instance count, the capabilities and the volume layout are
all decisions about the catalogue, not about the fleet — and `makersbrain-infra`
had no `.container` files to slot them into. Installing them is an
infrastructure step; writing them is not.

## Installing

```sh
install -d -m 0755 /etc/containers/systemd
install -m 0644 deploy/quadlet/*.container /etc/containers/systemd/
install -m 0644 deploy/quadlet/*.volume    /etc/containers/systemd/
systemctl daemon-reload

systemctl enable --now catalogue-service catalogue-control
systemctl enable --now catalogue-nats catalogue-dispatcher
systemctl enable --now catalogue-worker@{1,2,3}
systemctl enable --now catalogue-worker-browser
```

`catalogue-worker@.service` is a template, so the instance count is
`systemctl enable catalogue-worker@{1,2,3}` and nothing else changes. The queue
does not care how many there are; `catalogue.hosts` is what stops three of them
tripling the load on every shop.

## Secrets

The database DSN and the control token come from Infisical through the existing
`scripts/infisical-populate.sh` path, written to `/etc/catalogue/catalogue.env`
with mode `0640`. Neither is in a unit file, and the control service refuses to
start without a token.

```sh
infisical-populate.sh --path /catalogue --out /etc/catalogue/catalogue.env
```

Set `CATALOGUE_QUEUE_PROVIDER` and the provider mapping in that environment
file. Put role-scoped queue credentials in `/etc/catalogue/queue-secrets` with
mode `0400`: `nats-{publish,consume,stats,admin}-token` or
`cloudflare-{publish,consume,recovery,stats,admin}-token`. Each unit mounts only
the credential files its process is allowed to open. The NATS unit is required
only for `CATALOGUE_QUEUE_PROVIDER=nats`; a
Cloudflare deployment does not enable it. Provisioning and provider switching
follow [the queue runbook](../../docs/queue-provider-runbook.md).

Because Quadlet mounts are static, create empty mode-`0400` placeholders for
the inactive provider's same-role files; adapters never open credentials for
the unselected provider. Never copy an admin credential into this directory:
use a separate administrative shell for `catalogue-queue-admin`.

## No timer

There is deliberately no `.timer`. The schedule is in-process behind a
transaction-scoped advisory lock, so whichever worker holds it materialises due
runs — one less unit to deploy and no single point of failure.

If a timer is preferred for visibility, it can `curl` the control API instead,
and the in-process leader is disabled by setting the schedule's `enabled` to
false. That is a choice to make when the units are installed, not before.

## Backups

`catalogue-dumps.volume` holds the NDJSON artifacts and **is** in the backup
set: each job records a `sha256` against a path in it, and without the file that
digest describes nothing.

`catalogue-cache.volume` is deliberately **not** backed up. It is hundreds of
megabytes, it is reproducible by fetching, and losing it costs one slow run.

The backup set is now implemented rather than only described:
`catalogue-backup` (image `docker/backup`) dumps PostgreSQL and this volume into
one restic repository, in that order, because artifacts are write-once and the
dump must not be able to reference a file the artifact pass never saw. Restore
is not complete until `catalogue-backup verify` re-checks every recorded
`sha256` against the restored files. See
[`docs/backup-restore-runbook.md`](../../docs/backup-restore-runbook.md).

The schedule, the object storage credentials and the Quadlet timer belong to
`mb-infra`; this repository owns the image and the knowledge of what has to stay
consistent with what.
