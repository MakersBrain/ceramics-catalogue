"""Short-lived normalized queue snapshot cache shared by API and Prometheus."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from mb_ceramics_catalogue.ops.delivery import QueueSnapshot, QueueStatsReader


class QueueSnapshotCache:
    def __init__(
        self, reader: QueueStatsReader, *, max_age_seconds: float = 5.0, timeout_seconds: float = 3.0
    ) -> None:
        self.reader = reader
        self.max_age_seconds = max_age_seconds
        self.timeout_seconds = timeout_seconds
        self._snapshot: QueueSnapshot | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> QueueSnapshot:
        cached = self._snapshot
        if cached is not None and self.observation_age(cached) < self.max_age_seconds:
            return cached
        async with self._lock:
            cached = self._snapshot
            if cached is not None and self.observation_age(cached) < self.max_age_seconds:
                return cached
            snapshot = await asyncio.wait_for(self.reader.snapshot(), timeout=self.timeout_seconds)
            self._snapshot = snapshot
            return snapshot

    @staticmethod
    def age(snapshot: QueueSnapshot) -> float:
        successful = snapshot.last_success_at
        if successful is None:
            return max(0.0, (datetime.now(UTC) - snapshot.observed_at).total_seconds())
        return max(0.0, (datetime.now(UTC) - successful).total_seconds())

    @staticmethod
    def observation_age(snapshot: QueueSnapshot) -> float:
        """Age of this attempt, independent of the provider's last success."""
        return max(0.0, (datetime.now(UTC) - snapshot.observed_at).total_seconds())

    async def close(self) -> None:
        await self.reader.close()
