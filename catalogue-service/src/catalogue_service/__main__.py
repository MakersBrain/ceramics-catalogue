"""`catalogue-service`: serve the catalogue read API."""

from __future__ import annotations

import uvicorn

from catalogue_service.app import DSN, PORT, create_app


def main() -> int:
    uvicorn.run(create_app(DSN), host="0.0.0.0", port=PORT, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
