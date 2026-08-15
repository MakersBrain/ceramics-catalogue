"""Priority-aware request planning without becoming a spending authority."""

from __future__ import annotations

from mb_ceramics_catalogue.connectors.budget import (
    ZERO_COST,
    BudgetDecision,
    RequestCost,
    RequestPriority,
)
from mb_ceramics_catalogue.observability import metrics


class RequestBudget:
    """Plans work beneath hard transport/proxy limits.

    The proxy reservation still enforces paid bytes.  This object only refuses
    or reshapes planned work early enough to preserve higher-priority reserves.
    """

    def __init__(
        self,
        limit: RequestCost,
        *,
        reserves: dict[RequestPriority, RequestCost] | None = None,
        ceilings: dict[RequestPriority, RequestCost] | None = None,
    ) -> None:
        self.limit = limit
        self.reserves = dict(reserves or {})
        self.ceilings = dict(ceilings or {})
        self.used = ZERO_COST
        self.used_by_priority: dict[RequestPriority, RequestCost] = {}

    def decide(
        self,
        priority: RequestPriority,
        cost: RequestCost,
        *,
        required: bool,
        alternative: RequestCost | None = None,
        future_reserve: RequestCost | None = None,
    ) -> BudgetDecision:
        if self._allows(priority, cost, future_reserve=future_reserve):
            decision = BudgetDecision.ALLOW
        elif alternative is not None and self._allows(priority, alternative, future_reserve=future_reserve):
            decision = BudgetDecision.DOWNGRADE
        else:
            decision = BudgetDecision.DENY if required else BudgetDecision.DEFER
        metrics.request_budget_decision(priority.name.lower(), decision.value)
        return decision

    def consume(self, priority: RequestPriority, actual: RequestCost) -> None:
        """Record measured usage; callers must still enforce external ledgers."""
        next_total = self.used + actual
        if next_total.exceeds(self.limit):
            raise ValueError("measured request usage exceeds the job hard limit")
        used_for_priority = self.used_by_priority.get(priority, ZERO_COST)
        ceiling = self.ceilings.get(priority)
        if ceiling is not None and (used_for_priority + actual).exceeds(ceiling):
            raise ValueError("measured request usage exceeds the priority ceiling")
        self.used = next_total
        self.used_by_priority[priority] = used_for_priority + actual

    def _allows(
        self,
        priority: RequestPriority,
        cost: RequestCost,
        *,
        future_reserve: RequestCost | None = None,
    ) -> bool:
        next_total = self.used + cost
        if future_reserve is not None:
            next_total += future_reserve
        if next_total.exceeds(self.limit):
            return False

        used_for_priority = self.used_by_priority.get(priority, ZERO_COST)
        ceiling = self.ceilings.get(priority)
        if ceiling is not None and (used_for_priority + cost).exceeds(ceiling):
            return False

        protected = ZERO_COST
        for protected_priority, reserve in self.reserves.items():
            if protected_priority >= priority:
                continue
            already_used = self.used_by_priority.get(protected_priority, ZERO_COST)
            protected += RequestCost(
                http_requests=max(0, reserve.http_requests - already_used.http_requests),
                browser_requests=max(0, reserve.browser_requests - already_used.browser_requests),
                proxy_bytes=max(0, reserve.proxy_bytes - already_used.proxy_bytes),
            )

        remaining = RequestCost(
            http_requests=self.limit.http_requests - next_total.http_requests,
            browser_requests=self.limit.browser_requests - next_total.browser_requests,
            proxy_bytes=self.limit.proxy_bytes - next_total.proxy_bytes,
        )
        return not protected.exceeds(remaining)
