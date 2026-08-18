from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mb_ceramics_catalogue.ops.delivery import Measurement, QueueSnapshot

from catalogue_control.queue_stats import QueueSnapshotCache


class SnapshotReader:
    provider = "nats"

    def __init__(self, snapshot: QueueSnapshot) -> None:
        self.value = snapshot
        self.calls = 0

    async def snapshot(self) -> QueueSnapshot:
        self.calls += 1
        return self.value

    async def close(self) -> None:
        return None


async def test_failed_snapshot_is_cached_by_observation_time() -> None:
    now = datetime.now(UTC)
    snapshot = QueueSnapshot(
        provider="nats",
        observed_at=now,
        last_success_at=now - timedelta(hours=1),
        available=False,
        backlog_messages=Measurement.unsupported(),
        backlog_bytes=Measurement.unsupported(),
        consumer_count=Measurement.unsupported(),
        routes=(),
        error="TimeoutError: unavailable",
    )
    reader = SnapshotReader(snapshot)
    cache = QueueSnapshotCache(reader, max_age_seconds=5)

    assert await cache.get() is snapshot
    assert await cache.get() is snapshot
    assert reader.calls == 1
    assert cache.age(snapshot) >= 3599
    assert cache.observation_age(snapshot) < 5
