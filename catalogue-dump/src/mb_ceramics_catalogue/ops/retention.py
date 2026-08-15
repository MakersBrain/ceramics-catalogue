"""Deterministic artifact retention selection and managed deletion."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

SELECT_CANDIDATES = """
with outputs as (
  select j.id, j.source_id, j.state, j.finished_at, a.location as artifact_path,
         a.id as artifact_id, d.dataset, true as usable
    from catalogue.jobs j
    join catalogue.job_datasets d on d.job_id = j.id
    join catalogue.job_artifacts a
      on (a.job_id, a.dataset, a.contract_version, a.projector_version) =
         (d.job_id, d.dataset, d.contract_version, d.projector_version)
   where a.available
     and d.state in ('succeeded', 'degraded') and d.complete
  union all
  select j.id, j.source_id, j.state, j.finished_at, j.artifact_path,
         null::uuid, 'legacy',
         j.state = 'succeeded' or (
           j.state = 'degraded' and j.summary->>'write_status' = 'replaced'
           and not coalesce((j.summary->>'truncated')::boolean, false)
           and not coalesce((j.summary->>'interrupted')::boolean, false)
         )
    from catalogue.jobs j
   where j.artifact_path is not null
     and not exists (select 1 from catalogue.job_datasets d where d.job_id = j.id)
), ranked as (
  select o.*,
         count(*) filter (where o.usable) over (
           partition by o.source_id, o.dataset
           order by o.finished_at desc nulls last, o.id desc
           rows between unbounded preceding and current row
         ) as successful_rank
    from outputs o
), candidates as (
  select r.*
    from ranked r
   where r.finished_at is not null
     and (
       (r.usable
        and r.successful_rank > 2
        and r.finished_at < now() - interval '14 days')
       or
       (r.state in ('degraded', 'failed', 'cancelled')
        and r.finished_at < now() - interval '30 days')
     )
)
select c.id, c.source_id, c.artifact_path, c.artifact_id
  from candidates c
 where not exists (
   select 1
     from ranked retained
    where retained.artifact_path = c.artifact_path
      and retained.id <> c.id
      and not exists (select 1 from candidates c2 where c2.id = retained.id)
 )
 order by c.finished_at, c.id
"""


@dataclass(frozen=True)
class RetentionTarget:
    job_id: str
    source_id: str
    path: Path
    bytes: int
    artifact_id: str | None = None


@dataclass(frozen=True)
class RetentionReport:
    targets: tuple[RetentionTarget, ...]
    missing: int = 0

    @property
    def files(self) -> int:
        return len(self.targets)

    @property
    def bytes(self) -> int:
        return sum(target.bytes for target in self.targets)


def select(connection: psycopg.Connection[dict[str, Any]], root: Path) -> RetentionReport:
    base = root.resolve()
    targets: list[RetentionTarget] = []
    missing = 0
    with connection.cursor() as cursor:
        cursor.execute(SELECT_CANDIDATES)
        rows = cursor.fetchall()
    for row in rows:
        recorded = Path(row["artifact_path"])
        path = recorded.resolve() if recorded.is_absolute() else (base / recorded).resolve()
        try:
            path.relative_to(base)
        except ValueError as error:
            raise ValueError(f"artifact path is outside retention root: {recorded}") from error
        if not path.exists():
            missing += 1
        targets.append(
            RetentionTarget(
                str(row["id"]), row["source_id"], path,
                path.stat().st_size if path.exists() else 0,
                str(row["artifact_id"]) if row["artifact_id"] else None,
            )
        )
    return RetentionReport(tuple(targets), missing)


def execute(connection: psycopg.Connection[dict[str, Any]], report: RetentionReport) -> None:
    """Mark references unavailable before removing the exact reviewed targets."""
    for target in report.targets:
        with connection.transaction(), connection.cursor() as cursor:
            if target.artifact_id is not None:
                cursor.execute(
                    """update catalogue.job_artifacts
                              set available = false, retained_at = now()
                            where id = %s and available""",
                    (target.artifact_id,),
                )
            else:
                cursor.execute(
                """update catalogue.jobs
                          set artifact_path = null,
                              summary = coalesce(summary, '{}'::jsonb) ||
                                jsonb_build_object('artifact_unavailable', true,
                                                   'artifact_retained_sha256', artifact_sha256)
                        where id = %s and artifact_path is not null""",
                (target.job_id,),
                )
            if cursor.rowcount != 1:
                raise RuntimeError(f"artifact retention target changed: {target.job_id}")
        # The committed reference change deliberately precedes unlink. A
        # crash between them leaves an unreferenced file that a later sweep can
        # remove; the opposite order leaves a live DB link to missing data.
        target.path.unlink(missing_ok=True)


def connect(dsn: str) -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
