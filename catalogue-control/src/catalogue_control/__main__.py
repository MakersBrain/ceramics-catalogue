"""`catalogue-control`: serve the operator API."""

from __future__ import annotations

import sys

import uvicorn

from catalogue_control.app import create_app
from catalogue_control.settings import Settings
from catalogue_control.telemetry import configure, get_logger


def main() -> int:
    settings = Settings()
    configure(settings.log_level, json=settings.log_json)
    log = get_logger("catalogue.control")

    try:
        app = create_app(settings)
    except ValueError as error:
        print(f"catalogue-control: {error}", file=sys.stderr)
        return 2

    log.info("control.starting", host=settings.host, port=settings.port)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        # uvicorn's own access log would duplicate the structured one and is
        # not JSON, so a shipper would see two formats from one process.
        access_log=False,
        log_config=None,
        # SSE connections are long-lived by design; the default keep-alive
        # timeout would close an idle stream between keepalives.
        timeout_keep_alive=65,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
