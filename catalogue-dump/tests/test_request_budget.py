from mb_ceramics_catalogue.connectors.budget import RequestGate
from mb_ceramics_catalogue.observability import metrics
from mb_ceramics_catalogue.pipeline.budget import (
    BudgetDecision,
    RequestBudget,
    RequestCost,
    RequestPriority,
)


def test_optional_work_cannot_consume_discovery_reserve() -> None:
    budget = RequestBudget(
        RequestCost(http_requests=10, browser_requests=2, proxy_bytes=1_000),
        reserves={RequestPriority.DISCOVERY: RequestCost(http_requests=3, proxy_bytes=300)},
    )

    assert (
        budget.decide(
            RequestPriority.OPTIONAL,
            RequestCost(http_requests=8, proxy_bytes=800),
            required=False,
        )
        == BudgetDecision.DEFER
    )
    assert (
        budget.decide(
            RequestPriority.DISCOVERY,
            RequestCost(http_requests=8, proxy_bytes=800),
            required=True,
        )
        == BudgetDecision.ALLOW
    )


def test_required_work_is_denied_before_stealing_a_higher_reserve() -> None:
    budget = RequestBudget(
        RequestCost(http_requests=5),
        reserves={RequestPriority.DISCOVERY: RequestCost(http_requests=2)},
    )
    assert (
        budget.decide(RequestPriority.DATASET_REQUIRED, RequestCost(http_requests=4), required=True)
        == BudgetDecision.DENY
    )


def test_a_cheaper_transport_can_be_selected_as_a_downgrade() -> None:
    budget = RequestBudget(RequestCost(http_requests=10, browser_requests=1))
    budget.consume(RequestPriority.DETAIL, RequestCost(browser_requests=1))

    assert (
        budget.decide(
            RequestPriority.DETAIL,
            RequestCost(browser_requests=1),
            required=True,
            alternative=RequestCost(http_requests=1),
        )
        == BudgetDecision.DOWNGRADE
    )


def test_priority_ceiling_and_measured_hard_limit_are_enforced() -> None:
    budget = RequestBudget(
        RequestCost(http_requests=3),
        ceilings={RequestPriority.OPTIONAL: RequestCost(http_requests=1)},
    )
    budget.consume(RequestPriority.OPTIONAL, RequestCost(http_requests=1))
    assert (
        budget.decide(RequestPriority.OPTIONAL, RequestCost(http_requests=1), required=False)
        == BudgetDecision.DEFER
    )

    budget.consume(RequestPriority.DISCOVERY, RequestCost(http_requests=2))
    try:
        budget.consume(RequestPriority.DISCOVERY, RequestCost(http_requests=1))
    except ValueError as error:
        assert "hard limit" in str(error)
    else:  # pragma: no cover - explicit failure reads better than pytest.raises here
        raise AssertionError("hard limit was not enforced")


def test_optional_work_preserves_an_explicit_future_discovery_request() -> None:
    budget = RequestBudget(RequestCost(http_requests=3, proxy_bytes=3_000_000))
    budget.consume(
        RequestPriority.DISCOVERY,
        RequestCost(http_requests=1, proxy_bytes=1_000_000),
    )
    detail = RequestCost(http_requests=1, proxy_bytes=250_000)
    reserve = RequestCost(http_requests=1, proxy_bytes=1_000_000)

    assert (
        budget.decide(
            RequestPriority.OPTIONAL,
            detail,
            required=False,
            future_reserve=reserve,
        )
        == BudgetDecision.ALLOW
    )
    budget.consume(RequestPriority.OPTIONAL, detail)
    assert (
        budget.decide(
            RequestPriority.OPTIONAL,
            detail,
            required=False,
            future_reserve=reserve,
        )
        == BudgetDecision.DEFER
    )


def test_budget_decisions_have_only_bounded_metric_labels() -> None:
    metrics.REGISTRY.clear()
    budget = RequestBudget(RequestCost(http_requests=1))

    assert (
        budget.decide(RequestPriority.DISCOVERY, RequestCost(http_requests=1), required=True)
        == BudgetDecision.ALLOW
    )

    rendered = metrics.render()
    assert 'priority="discovery"' in rendered
    assert 'decision="allow"' in rendered


def test_request_gate_atomically_accounts_only_allowed_or_downgraded_work() -> None:
    budget = RequestBudget(RequestCost(http_requests=1, browser_requests=1))
    gate = RequestGate(budget)

    assert gate.claim(
        RequestPriority.DETAIL,
        RequestCost(browser_requests=1),
        required=True,
    ) == BudgetDecision.ALLOW
    assert gate.claim(
        RequestPriority.OPTIONAL,
        RequestCost(http_requests=1),
        required=False,
        future_reserve=RequestCost(http_requests=1),
    ) == BudgetDecision.DEFER
    assert budget.used == RequestCost(browser_requests=1)


def test_measured_usage_cannot_cross_a_priority_ceiling() -> None:
    budget = RequestBudget(
        RequestCost(http_requests=3),
        ceilings={RequestPriority.DETAIL: RequestCost(http_requests=1)},
    )
    budget.consume(RequestPriority.DETAIL, RequestCost(http_requests=1))

    try:
        budget.consume(RequestPriority.DETAIL, RequestCost(http_requests=1))
    except ValueError as error:
        assert "priority ceiling" in str(error)
    else:  # pragma: no cover
        raise AssertionError("priority ceiling was not enforced")
