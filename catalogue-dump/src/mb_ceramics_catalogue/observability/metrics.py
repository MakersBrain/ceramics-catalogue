"""The numbers the crawl already computes and currently throws away.

`HostLimiter` decides a backoff and a slot count on every response; `Fetcher`
knows whether a page came off the wire or out of the cache; `record.coverage`
counts which fields a scraper actually filled. All of it is discarded when the
process exits.

Defined here as a small registry rendering Prometheus text, rather than through
a client library: this is a deliberately bounded set of instruments behind one
endpoint, and a dependency would be more to audit than the thing it serves. The
optional observability compose profile scrapes each worker separately, while
control publishes the authoritative database-backed fleet snapshot.
"""

from __future__ import annotations

import math
import threading
from collections import defaultdict
from typing import Literal, cast

Labels = tuple[tuple[str, str], ...]
Kind = Literal["counter", "gauge", "histogram"]

#: Seconds. Chosen for the two questions actually asked of this pipeline: "is
#: this host slow" (the middle of the range) and "is something hanging" (the
#: tail), so the buckets are dense around a second and reach a minute.
DURATION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})


def _labels(values: dict[str, str | None]) -> Labels:
    """Normalise a label set, dropping the ones that were not supplied.

    An absent label and an empty one are different series in Prometheus, and
    emitting `source=""` for every worker-level metric would double the
    cardinality for nothing.
    """
    return tuple(sorted((key, str(value)) for key, value in values.items() if value is not None))


def _escape(value: str) -> str:
    return value.replace("\\", r"\\").replace('"', r"\"").replace("\n", r"\n")


class Metric:
    """One named family: a help string, a kind, and its labelled series."""

    def __init__(self, name: str, kind: Kind, help_text: str, buckets: tuple[float, ...] = ()) -> None:
        self.name = name
        self.kind = kind
        self.help = help_text
        self.buckets = buckets
        self.values: dict[Labels, float] = defaultdict(float)
        #: histogram only: per-series bucket counts, sum and count.
        self.observations: dict[Labels, list[float]] = defaultdict(lambda: [0.0] * (len(buckets) + 2))

    def add(self, amount: float, labels: Labels) -> None:
        self.values[labels] += amount

    def set(self, amount: float, labels: Labels) -> None:
        self.values[labels] = amount

    def observe(self, amount: float, labels: Labels) -> None:
        row = self.observations[labels]
        for index, bound in enumerate(self.buckets):
            if amount <= bound:
                row[index] += 1
        row[-2] += amount  # sum
        row[-1] += 1  # count

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {self.kind}"]
        if self.kind == "histogram":
            for labels, row in sorted(self.observations.items()):
                # `observe` increments every bucket whose bound the value fits
                # under, so each cell is already the cumulative count Prometheus
                # expects rather than a per-bucket one to be summed here.
                for index, bound in enumerate(self.buckets):
                    lines.append(
                        f"{self.name}_bucket{self._render_labels(labels, le=_number(bound))} {row[index]:g}"
                    )
                lines.append(f"{self.name}_bucket{self._render_labels(labels, le='+Inf')} {row[-1]:g}")
                lines.append(f"{self.name}_sum{self._render_labels(labels)} {row[-2]:g}")
                lines.append(f"{self.name}_count{self._render_labels(labels)} {row[-1]:g}")
            return lines
        for labels, value in sorted(self.values.items()):
            lines.append(f"{self.name}{self._render_labels(labels)} {value:g}")
        return lines

    def _render_labels(self, labels: Labels, **extra: str) -> str:
        pairs = [f'{key}="{_escape(value)}"' for key, value in labels]
        pairs += [f'{key}="{_escape(value)}"' for key, value in extra.items()]
        return "{" + ",".join(pairs) + "}" if pairs else ""


def _number(value: float) -> str:
    return "+Inf" if math.isinf(value) else f"{value:g}"


class Registry:
    """Every instrument in this process, and the text `/metrics` returns."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._metrics: dict[str, Metric] = {}

    def _metric(self, name: str, kind: Kind, help_text: str, buckets: tuple[float, ...] = ()) -> Metric:
        metric = self._metrics.get(name)
        if metric is None:
            metric = Metric(name, kind, help_text, buckets)
            self._metrics[name] = metric
        return metric

    def counter(self, name: str, help_text: str, amount: float = 1.0, **labels: str | None) -> None:
        with self._lock:
            self._metric(name, "counter", help_text).add(amount, _labels(labels))

    def gauge(self, name: str, help_text: str, value: float, **labels: str | None) -> None:
        with self._lock:
            self._metric(name, "gauge", help_text).set(value, _labels(labels))

    def gauge_add(self, name: str, help_text: str, amount: float, **labels: str | None) -> None:
        with self._lock:
            self._metric(name, "gauge", help_text).add(amount, _labels(labels))

    def replace_gauge(
        self,
        name: str,
        help_text: str,
        series: list[tuple[float, dict[str, str | None]]],
    ) -> None:
        """Replace a database-backed gauge family with one complete snapshot."""
        with self._lock:
            metric = self._metric(name, "gauge", help_text)
            metric.values.clear()
            for value, labels in series:
                metric.set(value, _labels(labels))

    def histogram(
        self,
        name: str,
        help_text: str,
        value: float,
        buckets: tuple[float, ...] = DURATION_BUCKETS,
        **labels: str | None,
    ) -> None:
        with self._lock:
            self._metric(name, "histogram", help_text, buckets).observe(value, _labels(labels))

    def render(self) -> str:
        with self._lock:
            blocks = [line for name in sorted(self._metrics) for line in self._metrics[name].render()]
        return "\n".join(blocks) + "\n"

    def clear(self) -> None:
        with self._lock:
            self._metrics.clear()


#: One per process, like `activity.ACTIVITY`.
REGISTRY = Registry()


# -- the instruments, named once so a typo is an import error ---------------
#
# Each of these answers a question that is currently unanswerable while a run is
# happening: which host is failing, which host is slow, whether a "fresh" run is
# actually replaying (§8), whether a source shrank (§6.6), what the browser path
# costs, whether we are being throttled, and which extractor is drifting.


def request(source: str | None, host: str, outcome: str) -> None:
    REGISTRY.counter(
        "catalogue_requests_total",
        "HTTP requests issued, by source, host and outcome.",
        source=source,
        host=host,
        outcome=outcome,
    )


def request_duration(host: str, seconds: float) -> None:
    REGISTRY.histogram(
        "catalogue_request_duration_seconds",
        "Wall time of one HTTP request, by host.",
        seconds,
        host=host,
    )


def cache(outcome: Literal["hit", "miss", "write"]) -> None:
    REGISTRY.counter(
        "catalogue_cache_total",
        "Response cache outcomes. A daily run that is mostly hits is replaying, not pricing.",
        outcome=outcome,
    )


def records(source: str, count: float) -> None:
    REGISTRY.gauge(
        "catalogue_records_collected",
        "Records a source produced in its most recent job.",
        count,
        source=source,
    )


def browser_render(source: str | None) -> None:
    REGISTRY.counter(
        "catalogue_browser_renders_total",
        "Pages rendered through the browser, which is the expensive path.",
        source=source,
    )


def host_backoff(host: str, seconds: float) -> None:
    REGISTRY.histogram(
        "catalogue_host_backoff_seconds",
        "Gap a host earned by failing. Rising values mean we are being throttled.",
        seconds,
        buckets=(0.5, 1.0, 2.0, 4.0, 8.0, 16.0),
        host=host,
    )


def http_error(host: str, status: int | str) -> None:
    REGISTRY.counter(
        "catalogue_http_errors_total",
        "Responses a host refused, by status. A 403/429 rate is a shop blocking us.",
        host=host,
        status=str(status),
    )


def job_duration(source: str, seconds: float, outcome: str) -> None:
    REGISTRY.histogram(
        "catalogue_job_duration_seconds",
        "Wall time of one source's job.",
        seconds,
        buckets=(1, 5, 15, 60, 300, 900, 1800, 3600),
        source=source,
        outcome=outcome,
    )


def run_duration(seconds: float) -> None:
    REGISTRY.histogram(
        "catalogue_run_duration_seconds",
        "Wall time of a whole run.",
        seconds,
        buckets=(60, 300, 900, 1800, 3600, 7200, 14400),
    )


def jobs(state: str, count: float) -> None:
    REGISTRY.gauge("catalogue_jobs", "Jobs in each state.", count, state=state)


def jobs_snapshot(counts: dict[str, int]) -> None:
    states = ("queued", "leased", "running", "paused", "succeeded", "degraded", "failed", "cancelled", "skipped")
    REGISTRY.replace_gauge(
        "catalogue_jobs",
        "Jobs in each state.",
        [(float(counts.get(state, 0)), {"state": state}) for state in states],
    )


def queue_oldest_age(seconds: float) -> None:
    REGISTRY.replace_gauge(
        "catalogue_queue_oldest_age_seconds",
        "How long the oldest currently eligible queued job has waited.",
        [(seconds, {})],
    )


def workers_snapshot(healthy: int, lost: int) -> None:
    REGISTRY.replace_gauge(
        "catalogue_workers",
        "Registered non-stopped workers by heartbeat health.",
        [(float(healthy), {"health": "healthy"}), (float(lost), {"health": "lost"})],
    )


def proxy_reservation(state: str, amount: float = 1.0) -> None:
    REGISTRY.gauge(
        "catalogue_proxy_reservations",
        "Proxy reservations by current state.",
        amount,
        state=state,
    )


def proxy_bytes(kind: str, value: float) -> None:
    REGISTRY.gauge(
        "catalogue_proxy_bytes",
        "Proxy traffic and budget byte levels.",
        value,
        kind=kind,
    )


def proxy_reconciliation(successful: bool) -> None:
    REGISTRY.counter(
        "catalogue_proxy_reconciliations_total",
        "Provider reconciliation outcomes.",
        outcome="success" if successful else "failure",
    )


def proxy_probe(outcome: str) -> None:
    REGISTRY.counter(
        "catalogue_proxy_probes_total",
        "Bounded paid-probe outcomes.",
        outcome=outcome,
    )


def sources_snapshot(rows: list[dict[str, str | float | int | None]]) -> None:
    REGISTRY.replace_gauge(
        "catalogue_source_overdue_seconds",
        "Seconds past the expected scheduled completion and grace period.",
        [(float(row["overdue"] or 0), {"source": str(row["source"])}) for row in rows],
    )
    REGISTRY.replace_gauge(
        "catalogue_source_success_state",
        "Whether an enabled scheduled source has ever produced a usable result.",
        [(float(row["succeeded"] or 0), {"source": str(row["source"])}) for row in rows],
    )
    REGISTRY.replace_gauge(
        "catalogue_source_records",
        "Records in the latest usable complete result.",
        [
            (float(cast(str | float | int, row["records"])), {"source": str(row["source"])})
            for row in rows
            if row.get("records") is not None
        ],
    )
    REGISTRY.replace_gauge(
        "catalogue_source_record_ratio",
        "Latest usable record count divided by the preceding usable count.",
        [
            (
                float(cast(str | float | int, row["record_ratio"])),
                {"source": str(row["source"])},
            )
            for row in rows
            if row.get("record_ratio") is not None
        ],
    )


def job_completed(source: str, outcome: str) -> None:
    REGISTRY.counter(
        "catalogue_jobs_completed_total",
        "Terminal jobs completed by source and bounded outcome.",
        source=source,
        outcome=outcome,
    )


def offers_written(count: float) -> None:
    REGISTRY.counter(
        "catalogue_offers_written_total",
        "Offer observations written to the database.",
        count,
    )


def pipeline_entities(connector: str, connector_version: str, count: float) -> None:
    REGISTRY.counter(
        "catalogue_pipeline_entities_total",
        "Neutral entities emitted by connector and bounded connector version.",
        count,
        connector=connector,
        connector_version=connector_version,
    )


def pipeline_records(dataset: str, contract_version: str, outcome: str, count: float) -> None:
    REGISTRY.counter(
        "catalogue_pipeline_records_total",
        "Dataset records projected per page, including stable projector outcomes.",
        count,
        dataset=dataset,
        contract_version=contract_version,
        outcome=outcome,
    )


def request_budget_decision(priority: str, decision: str) -> None:
    REGISTRY.counter(
        "catalogue_request_budget_decisions_total",
        "Request planning decisions by bounded priority and decision.",
        priority=priority,
        decision=decision,
    )


def http_request(
    service: str, method: str, route: str, status: int, seconds: float | None
) -> None:
    bounded_method = method.upper()
    if bounded_method not in HTTP_METHODS:
        bounded_method = "OTHER"
    status_class = f"{status // 100}xx" if 200 <= status < 600 else "5xx"
    REGISTRY.counter(
        "catalogue_http_requests_total",
        "Inbound HTTP requests by service, method, route template and status class.",
        service=service,
        method=bounded_method,
        route=route,
        status_class=status_class,
    )
    if seconds is not None:
        REGISTRY.histogram(
            "catalogue_http_request_duration_seconds",
            "Inbound HTTP request wall time, excluding long-lived streams.",
            seconds,
            service=service,
            method=bounded_method,
            route=route,
        )


def http_request_in_flight(service: str, amount: float) -> None:
    REGISTRY.gauge_add(
        "catalogue_http_requests_in_flight",
        "Inbound HTTP requests currently being handled.",
        amount,
        service=service,
    )


def render() -> str:
    return REGISTRY.render()
