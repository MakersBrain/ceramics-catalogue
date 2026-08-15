"""Connector-neutral request classification and bounded accounting."""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from .base import Diagnostic, DiagnosticCode, DiagnosticSeverity, SnapshotField


class RequestPriority(IntEnum):
    DISCOVERY = 1
    IDENTITY = 2
    DATASET_REQUIRED = 3
    DETAIL = 4
    OPTIONAL = 5


class BudgetDecision(StrEnum):
    ALLOW = "allow"
    DOWNGRADE = "downgrade"
    DEFER = "defer"
    DENY = "deny"


class RequestCost(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    http_requests: int = Field(default=0, ge=0)
    browser_requests: int = Field(default=0, ge=0)
    proxy_bytes: int = Field(default=0, ge=0)

    def __add__(self, other: RequestCost) -> RequestCost:
        return RequestCost(
            http_requests=self.http_requests + other.http_requests,
            browser_requests=self.browser_requests + other.browser_requests,
            proxy_bytes=self.proxy_bytes + other.proxy_bytes,
        )

    def exceeds(self, limit: RequestCost) -> bool:
        return (
            self.http_requests > limit.http_requests
            or self.browser_requests > limit.browser_requests
            or self.proxy_bytes > limit.proxy_bytes
        )


ZERO_COST = RequestCost()


class RequestBudgetProtocol(Protocol):
    def decide(
        self,
        priority: RequestPriority,
        cost: RequestCost,
        *,
        required: bool,
        alternative: RequestCost | None = None,
        future_reserve: RequestCost | None = None,
    ) -> BudgetDecision: ...

    def consume(self, priority: RequestPriority, actual: RequestCost) -> None: ...


class RequestGate:
    def __init__(self, budget: RequestBudgetProtocol | None) -> None:
        self.budget = budget

    def claim(
        self,
        priority: RequestPriority,
        cost: RequestCost,
        *,
        required: bool,
        alternative: RequestCost | None = None,
        future_reserve: RequestCost | None = None,
    ) -> BudgetDecision:
        if self.budget is None:
            return BudgetDecision.ALLOW
        decision = self.budget.decide(
            priority,
            cost,
            required=required,
            alternative=alternative,
            future_reserve=future_reserve,
        )
        if decision == BudgetDecision.ALLOW:
            self.budget.consume(priority, cost)
        elif decision == BudgetDecision.DOWNGRADE:
            assert alternative is not None
            self.budget.consume(priority, alternative)
        return decision


class ConnectorBudget:
    """Small connector-facing facade over the shared planning budget."""

    def __init__(self, budget: RequestBudgetProtocol | None = None) -> None:
        self._gate = RequestGate(budget)

    @property
    def budget(self) -> RequestBudgetProtocol | None:
        """Expose the configured planner for wiring validation and observability."""
        return self._gate.budget

    def claim(
        self,
        priority: RequestPriority,
        *,
        required: bool,
        browser: bool = False,
        proxy_bytes: int = 0,
        future_reserve: RequestCost | None = None,
    ) -> BudgetDecision:
        return self._gate.claim(
            priority,
            RequestCost(
                http_requests=0 if browser else 1,
                browser_requests=1 if browser else 0,
                proxy_bytes=proxy_bytes,
            ),
            required=required,
            future_reserve=future_reserve,
        )

    def required_detail_priority(
        self,
        requested_fields: frozenset[SnapshotField],
        supplied_fields: frozenset[SnapshotField],
    ) -> RequestPriority:
        """Make dataset union drive detail importance without naming datasets."""
        return (
            RequestPriority.DATASET_REQUIRED
            if requested_fields & supplied_fields
            else RequestPriority.DETAIL
        )

    def require(
        self,
        priority: RequestPriority,
        url: str,
        *,
        browser: bool = False,
        proxy_bytes: int = 0,
        future_reserve: RequestCost | None = None,
    ) -> None:
        if self.claim(
            priority,
            required=True,
            browser=browser,
            proxy_bytes=proxy_bytes,
            future_reserve=future_reserve,
        ) not in {BudgetDecision.ALLOW, BudgetDecision.DOWNGRADE}:
            raise BudgetExhausted(priority, url)

    def optional(
        self,
        priority: RequestPriority,
        *,
        browser: bool = False,
        proxy_bytes: int = 0,
        future_reserve: RequestCost | None = None,
    ) -> bool:
        return self.claim(
            priority,
            required=False,
            browser=browser,
            proxy_bytes=proxy_bytes,
            future_reserve=future_reserve,
        ) in {BudgetDecision.ALLOW, BudgetDecision.DOWNGRADE}


class BudgetExhausted(RuntimeError):
    def __init__(self, priority: RequestPriority, url: str) -> None:
        super().__init__(f"request budget exhausted for {priority.name.lower()}")
        self.priority = priority
        self.url = url


def budget_diagnostic(priority: RequestPriority, url: str) -> Diagnostic:
    return Diagnostic(
        code=DiagnosticCode.REQUEST_BUDGET_EXHAUSTED,
        severity=DiagnosticSeverity.ERROR,
        message=f"request budget cannot fund required {priority.name.lower()} work",
        retryable=True,
        affects_completeness=True,
        url=url,
    )


__all__ = [
    "ZERO_COST",
    "BudgetDecision",
    "BudgetExhausted",
    "ConnectorBudget",
    "RequestBudgetProtocol",
    "RequestCost",
    "RequestGate",
    "RequestPriority",
    "budget_diagnostic",
]
