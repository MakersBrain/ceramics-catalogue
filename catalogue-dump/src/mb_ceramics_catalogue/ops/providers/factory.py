"""One explicit provider factory shared by worker, dispatcher, and control."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Any

from mb_ceramics_catalogue.ops.delivery import (
    QueueConsumer,
    QueueProvisioner,
    QueuePublisher,
    QueueStatsReader,
)
from mb_ceramics_catalogue.ops.providers.cloudflare import (
    CloudflareConsumer,
    CloudflareProvisioner,
    CloudflarePublisher,
    CloudflareQueueClient,
    CloudflareRecoveryConsumer,
    CloudflareStatsReader,
)
from mb_ceramics_catalogue.ops.providers.nats import (
    NatsConsumer,
    NatsProvisioner,
    NatsPublisher,
    NatsStatsReader,
)


class QueueProviderName(StrEnum):
    NATS = "nats"
    CLOUDFLARE = "cloudflare"


def publisher(settings: Any) -> QueuePublisher:
    selected = QueueProviderName(settings.queue_provider)
    if selected is QueueProviderName.NATS:
        auth = _nats_auth(settings, "publish")
        return NatsPublisher(
            settings.nats_url,
            **auth,
            stream=settings.nats_stream,
        )
    return CloudflarePublisher(_cf_api(settings, settings.cf_publish_token_file), _cf_routes(settings))


def consumer(settings: Any) -> QueueConsumer:
    selected = QueueProviderName(settings.queue_provider)
    if selected is QueueProviderName.NATS:
        auth = _nats_auth(settings, "consume")
        return NatsConsumer(
            settings.nats_url,
            **auth,
            stream=settings.nats_stream,
        )
    return CloudflareConsumer(
        _cf_api(settings, settings.cf_consume_token_file),
        _cf_routes(settings),
        visibility_seconds=settings.queue_visibility_seconds,
        max_retries=settings.cf_max_retries,
        empty_poll_seconds=settings.queue_poll_empty_seconds,
    )


def stats_reader(settings: Any) -> QueueStatsReader:
    selected = QueueProviderName(settings.queue_provider)
    if selected is QueueProviderName.NATS:
        return NatsStatsReader(
            settings.nats_url, **_nats_auth(settings, "stats"), stream=settings.nats_stream
        )
    return CloudflareStatsReader(
        _cf_api(settings, settings.cf_stats_token_file),
        _cf_routes(settings),
        settings.cf_queue_recovery_dlq_id,
    )


def recovery_consumer(settings: Any) -> CloudflareRecoveryConsumer | None:
    if QueueProviderName(settings.queue_provider) is QueueProviderName.NATS:
        return None
    return CloudflareRecoveryConsumer(
        _cf_api(settings, settings.cf_recovery_token_file),
        settings.cf_queue_recovery_dlq_id,
        visibility_seconds=settings.queue_visibility_seconds,
    )


def provisioner(settings: Any) -> QueueProvisioner:
    selected = QueueProviderName(settings.queue_provider)
    if selected is QueueProviderName.NATS:
        return NatsProvisioner(
            settings.nats_url, **_nats_auth(settings, "admin"), stream=settings.nats_stream
        )
    return CloudflareProvisioner(
        _cf_api(settings, settings.cf_admin_token_file),
        _cf_routes(settings),
        settings.cf_queue_recovery_dlq_id,
        visibility_seconds=settings.queue_visibility_seconds,
        max_retries=settings.cf_max_retries,
    )


def _cf_api(settings: Any, token_file: Path | None) -> CloudflareQueueClient:
    if token_file is None:
        raise ValueError("Cloudflare queue credential file is not configured for this role")
    return CloudflareQueueClient(
        settings.cf_account_id,
        _secret(token_file),
        base_url=settings.cf_api_base_url,
    )


def _cf_routes(settings: Any) -> dict[str, str]:
    return {
        "plain.normal": settings.cf_queue_plain_id,
        "browser.auto.normal": settings.cf_queue_browser_auto_id,
        "browser.camoufox.normal": settings.cf_queue_browser_camoufox_id,
        "browser.cdp_extension_proxy.normal": settings.cf_queue_browser_cdp_extension_proxy_id,
    }


def _secret(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ValueError(f"cannot read queue credential file {path}") from error
    if not value:
        raise ValueError(f"queue credential file is empty: {path}")
    return value


def _nats_auth(settings: Any, role: str) -> dict[str, str]:
    credentials_file = getattr(settings, f"nats_{role}_credentials_file", None)
    if credentials_file is None:
        token_file = getattr(settings, f"nats_{role}_token_file", None)
        return {"token": _secret(token_file)}
    try:
        document = json.loads(credentials_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read NATS {role} credentials file") from error
    if set(document) != {"user", "password"}:
        raise ValueError(f"NATS {role} credentials must contain only user and password")
    user = document["user"]
    password = document["password"]
    if not isinstance(user, str) or not user or not isinstance(password, str) or len(password) < 32:
        raise ValueError(f"NATS {role} credentials are invalid")
    return {"user": user, "password": password}
