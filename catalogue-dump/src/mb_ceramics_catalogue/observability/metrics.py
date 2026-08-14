"""The numbers the crawl already computes and currently throws away.

`HostLimiter` decides a backoff and a slot count on every response; `Fetcher`
knows whether a page came off the wire or out of the cache; `record.coverage`
counts which fields a scraper actually filled. All of it is discarded when the
process exits.

Defined here as a small registry rendering Prometheus text, rather than through
a client library, for the reason §9 gives: this is a handful of instruments
behind one endpoint, and a dependency would be more to audit than the thing it
serves. The shape is standard, so swapping in a real client later is mechanical.

Nothing scrapes these yet. They exist so that when something does, no rework is
needed — and `/ops/metrics` reads the same quantities straight from Postgres in
the meantime.
"""

from __future__ import annotations

import math
import threading
from collections import defaultdict
from typing import Literal

Labels = tuple[tuple[str, str], ...]
Kind = Literal["counter", "gauge", "histogram"]

#: Seconds. Chosen for the two questions actually asked of this pipeline: "is
#: this host slow" (the middle of the range) and "is something hanging" (the
#: tail), so the buckets are dense around a second and reach a minute.
DURATION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)


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


def parse_failure(source: str, field: str) -> None:
    REGISTRY.counter(
        "catalogue_parse_failures_total",
        "Fields an extractor could not read, by source and field.",
        source=source,
        field=field,
    )


def job_duration(source: str, seconds: float) -> None:
    REGISTRY.histogram(
        "catalogue_job_duration_seconds",
        "Wall time of one source's job.",
        seconds,
        buckets=(1, 5, 15, 60, 300, 900, 1800, 3600),
        source=source,
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


def worker_heartbeat_age(worker: str, seconds: float) -> None:
    REGISTRY.gauge(
        "catalogue_worker_heartbeat_age_seconds",
        "How long since a worker last reported. Rising without bound means it died.",
        seconds,
        worker=worker,
    )


def proxy_reservation(state: str, amount: float = 1.0) -> None:
    REGISTRY.gauge(
        "catalogue_proxy_reservations", "Proxy reservations by current state.",
        amount, state=state,
    )


def proxy_bytes(kind: str, value: float) -> None:
    REGISTRY.gauge(
        "catalogue_proxy_bytes", "Proxy traffic and budget byte levels.",
        value, kind=kind,
    )


def proxy_reconciliation(successful: bool) -> None:
    REGISTRY.counter(
        "catalogue_proxy_reconciliations_total", "Provider reconciliation outcomes.",
        outcome="success" if successful else "failure",
    )


def proxy_probe(outcome: str) -> None:
    REGISTRY.counter(
        "catalogue_proxy_probes_total", "Bounded paid-probe outcomes.", outcome=outcome,
    )


def source_staleness(source: str, seconds: float) -> None:
    REGISTRY.gauge(
        "catalogue_source_staleness_seconds",
        "How long since a source last produced any records.",
        seconds,
        source=source,
    )


def offers_written(count: float) -> None:
    REGISTRY.counter(
        "catalogue_offers_written_total",
        "Offer observations written to the database.",
        count,
    )


def render() -> str:
    return REGISTRY.render()
