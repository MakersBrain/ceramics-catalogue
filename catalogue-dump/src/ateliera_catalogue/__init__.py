"""The ceramics catalogue pipeline: collection, loading and the worker around them.

Nothing but the version lives here. A package `__init__` that imports its
subpackages makes `import ateliera_catalogue` pull in httpx, textual and a
Postgres driver, which is the wrong cost for a worker that only wants
`__version__` to write into `catalogue.workers`.
"""

from __future__ import annotations

__version__ = "0.2.0"

__all__ = ["__version__"]
