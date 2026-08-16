"""`catalogue-service`: serve the catalogue read API."""

from __future__ import annotations

import uvicorn

from catalogue_service.app import DSN, PORT, create_app
from catalogue_service.telemetry import configure, get_logger


def main() -> int:
    configure()
    get_logger().info("service.starting", host="0.0.0.0", port=PORT)
    uvicorn.run(
        create_app(DSN), host="0.0.0.0", port=PORT, access_log=False, log_config=None
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
