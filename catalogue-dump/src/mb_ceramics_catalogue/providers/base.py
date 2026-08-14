"""Provider-neutral models used by control, tests, and maintenance commands."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class ProviderError(RuntimeError):
    """A sanitized provider failure safe to expose as an RFC 9457 detail."""

    def __init__(self, code: str, message: str, *, ambiguous: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.ambiguous = ambiguous


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Subscription(StrictModel):
    provider_resource_id: str | None = None
    service_type: str
    traffic_limit_bytes: int | None = Field(default=None, ge=0)
    raw_traffic_limit: str | float | int | None = None
    valid_from: datetime
    valid_until: datetime
    users_limit: int | None = Field(default=None, ge=0)


class SubUser(StrictModel):
    id: str
    username: str
    status: str
    traffic_bytes: int | None = Field(default=None, ge=0)
    traffic_limit_bytes: int | None = Field(default=None, ge=0)
    auto_disable: bool = False
    traffic_count_from: datetime | None = None


class UsageBucket(StrictModel):
    key: str
    transmitted_bytes: int = Field(default=0, ge=0)
    received_bytes: int = Field(default=0, ge=0)
    total_bytes: int = Field(default=0, ge=0)
    requests: int = Field(default=0, ge=0)


class UsageReport(StrictModel):
    total_transmitted_bytes: int = Field(default=0, ge=0)
    total_received_bytes: int = Field(default=0, ge=0)
    total_bytes: int = Field(default=0, ge=0)
    requests: int = Field(default=0, ge=0)
    buckets: list[UsageBucket] = Field(default_factory=list)


class ProxyProvider(Protocol):
    async def health(self) -> bool: ...

    async def subscription(self) -> Subscription: ...

    async def usage(self, start: datetime, end: datetime, *, group_by: str = "day") -> UsageReport: ...

    async def list_subusers(self) -> list[SubUser]: ...

    async def create_subuser(
        self, *, username: str, password: str, traffic_limit_bytes: int,
        traffic_count_from: datetime,
    ) -> SubUser: ...

    async def update_subuser(
        self, resource_id: str, *, password: str | None = None,
        traffic_limit_bytes: int | None = None, status: str | None = None,
    ) -> SubUser: ...

    async def delete_subuser(self, resource_id: str) -> None: ...
