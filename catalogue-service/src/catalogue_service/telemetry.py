"""Shared structured logging and Prometheus rendering for the read service."""

from __future__ import annotations

from typing import Any

from mb_ceramics_catalogue.observability import logging as obs
from mb_ceramics_catalogue.observability import metrics


def configure(level: str = "INFO", *, json: bool | None = None) -> None:
    obs.configure(level, json=json)


def get_logger(name: str = "catalogue.service") -> Any:
    return obs.get_logger(name)


def render_metrics() -> str:
    return metrics.render()
