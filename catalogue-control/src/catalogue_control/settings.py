"""Where the control service runs, from the environment."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CATALOGUE_", env_file=".env", extra="ignore"
    )

    dsn: str = ""
    #: Required on every `/v1` route including the stream. `/health` and
    #: `/metrics` are the only exemptions.
    #:
    #: The service is additionally not published on the host, which is defence
    #: in depth rather than its authentication boundary — an unauthenticated
    #: service reachable from one network is still an unauthenticated service.
    control_token: str = ""
    host: str = "0.0.0.0"
    port: int = 8687
    log_level: str = "INFO"
    log_json: bool | None = None
    artifacts_dir: Path = Path("/var/lib/catalogue/dumps")
    proxy_enabled: bool = False
    proxy_api_secret_file: Path | None = None
    proxy_secret_file: Path | None = None
    proxy_actor_public_keys_file: Path | None = None
    proxy_provider_limit_unit: Literal["unconfirmed", "decimal_gb"] = "unconfirmed"
    proxy_provider_base_url: str = "https://api.decodo.com"
    proxy_reconcile_interval_seconds: float = 3600
    proxy_mutations_enabled: bool = False
    proxy_paid_probe_enabled: bool = False

    #: Refuse to start without a token rather than serving an open control
    #: plane. The one thing worse than no run-cancel endpoint is an
    #: unauthenticated one.
    require_token: bool = True
