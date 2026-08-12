"""Logging and metrics for the control service.

Both are borrowed from the collector's package rather than reimplemented, so a
`job.failed` line looks the same whichever process wrote it and `/metrics` has
one format across the deployment.
"""

from __future__ import annotations

from typing import Any

from mb_ceramics_catalogue.observability import logging as obs
from mb_ceramics_catalogue.observability import metrics


def configure(level: str = "INFO", *, json: bool | None = None) -> None:
    obs.configure(level, json=json)


def get_logger(name: str = "catalogue.control") -> Any:
    return obs.get_logger(name)


def render_metrics() -> str:
    return metrics.render()


__all__ = ["configure", "get_logger", "metrics", "render_metrics"]
