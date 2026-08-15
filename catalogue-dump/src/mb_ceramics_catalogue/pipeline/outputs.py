"""Bounded, deterministic storage for projected page batches.

A connector cursor may advance only after the database records every staged
batch for that page.  This module owns the filesystem half of that protocol: it
writes immutable, checksummed objects without retaining a complete source in
memory.  PostgreSQL owns the atomic page-manifest/cursor transaction.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


def _component(value: str) -> str:
    if not SAFE_COMPONENT.fullmatch(value) or value in {".", ".."}:
        raise ValueError(f"unsafe artifact component: {value!r}")
    return value


def _opaque_component(value: str) -> str:
    if not value or len(value.encode("utf-8")) > 2048:
        raise ValueError("opaque artifact identity must contain 1..2048 UTF-8 bytes")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_line(record: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        + "\n"
    ).encode("utf-8")


def _digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stored:
        for chunk in iter(lambda: stored.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


@dataclass(frozen=True)
class BatchIdentity:
    """The idempotency identity of one projected dataset page."""

    job_id: str
    checkpoint_lineage: str
    partition_key: str
    page_id: str
    page_sequence: int
    dataset: str
    contract_version: str
    projector_version: str

    def components(self) -> tuple[str, ...]:
        if self.page_sequence < 0:
            raise ValueError("page sequence must be non-negative")
        return (
            _component(self.job_id),
            _component(self.checkpoint_lineage),
            _opaque_component(self.partition_key),
            _opaque_component(self.page_id),
            _component(self.dataset),
            _component(self.contract_version),
            _component(self.projector_version),
        )


@dataclass(frozen=True)
class StoredObject:
    location: str
    sha256: str
    size: int
    records: int


@dataclass(frozen=True)
class StoredBatch(StoredObject):
    identity: BatchIdentity


class ArtifactStore(Protocol):
    """Storage operations used by the page commit protocol."""

    def stage_batch(
        self, identity: BatchIdentity, records: Iterable[Mapping[str, Any]]
    ) -> StoredBatch: ...

    def publish_dataset(
        self,
        job_id: str,
        dataset: str,
        contract_version: str,
        batches: Sequence[StoredBatch],
    ) -> StoredObject: ...


class LocalArtifactStore:
    """Local immutable object store with bounded streaming compaction."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def stage_batch(
        self, identity: BatchIdentity, records: Iterable[Mapping[str, Any]]
    ) -> StoredBatch:
        job, lineage, partition, page, dataset, contract, projector = identity.components()
        directory = self.root / "staging" / job / lineage / partition / f"{identity.page_sequence:012d}-{page}"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{dataset}.{contract}.{projector}.ndjson.gz"
        descriptor, name = tempfile.mkstemp(prefix="stage-", suffix=".ndjson.gz.tmp", dir=directory)
        os.close(descriptor)
        temporary = Path(name)
        try:
            count = self._write(temporary, records)
            sha256, size = _digest(temporary)

            if target.exists():
                existing_sha, existing_size = _digest(target)
                if existing_sha != sha256:
                    raise ValueError(f"staged batch changed for {identity!r}")
                return StoredBatch(str(target), existing_sha, existing_size, count, identity)

            temporary.replace(target)
            return StoredBatch(str(target), sha256, size, count, identity)
        finally:
            temporary.unlink(missing_ok=True)

    def publish_dataset(
        self,
        job_id: str,
        dataset: str,
        contract_version: str,
        batches: Sequence[StoredBatch],
    ) -> StoredObject:
        safe_job = _component(job_id)
        safe_dataset = _component(dataset)
        safe_contract = _component(contract_version)
        directory = self.root / "published" / safe_job
        directory.mkdir(parents=True, exist_ok=True)

        descriptor, name = tempfile.mkstemp(prefix="publish-", suffix=".ndjson.gz.tmp", dir=directory)
        os.close(descriptor)
        temporary = Path(name)
        records = 0
        try:
            records = self._write(temporary, self._records(batches))
            sha256, size = _digest(temporary)
            target = directory / f"{safe_dataset}.{safe_contract}.{sha256}.ndjson.gz"
            if target.exists():
                existing_sha, existing_size = _digest(target)
                if existing_sha != sha256:
                    raise ValueError(f"published artifact checksum mismatch at {target}")
                temporary.unlink(missing_ok=True)
                return StoredObject(str(target), existing_sha, existing_size, records)
            temporary.replace(target)
            return StoredObject(str(target), sha256, size, records)
        finally:
            temporary.unlink(missing_ok=True)

    def _records(self, batches: Sequence[StoredBatch]) -> Iterator[Mapping[str, Any]]:
        seen: set[BatchIdentity] = set()
        for batch in batches:
            if batch.identity in seen:
                raise ValueError(f"duplicate page batch: {batch.identity!r}")
            seen.add(batch.identity)
            path = self._local_path(batch.location)
            sha256, size = _digest(path)
            if sha256 != batch.sha256 or size != batch.size:
                raise ValueError(f"staged batch checksum mismatch: {batch.location}")
            with gzip.open(path, "rt", encoding="utf-8") as source:
                for number, line in enumerate(source, 1):
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError(f"batch {batch.location} line {number} is not an object")
                    yield value

    def _local_path(self, location: str) -> Path:
        path = Path(location).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as error:
            raise ValueError(f"artifact location is outside store root: {location}") from error
        return path

    @staticmethod
    def _write(path: Path, records: Iterable[Mapping[str, Any]]) -> int:
        count = 0
        with path.open("wb") as raw, gzip.GzipFile(
            filename="", mode="wb", compresslevel=6, fileobj=raw, mtime=0
        ) as encoded:
            for record in records:
                encoded.write(_json_line(record))
                count += 1
        return count
