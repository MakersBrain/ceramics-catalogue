"""An on-disk cache of what each site actually sent back.

Collection and interpretation are separate problems that happen to run in one
command. Parsing changes far more often than the pages do — a new field, a
corrected unit, a scope rule — and re-fetching a thousand product pages to test
a regex is both slow and rude to the shop.

So every response is written down as it arrived. A later run replays those
bytes instead of asking again, which makes reparsing a local operation, makes an
interrupted run resumable at the cost of a disk read, and makes a parser bug
reproducible from the exact page that triggered it.

Nothing here decides *whether* to use the cache; that is the run's mode:

    auto     use a stored response when it is younger than the max age
    replay   use only stored responses, and fail on anything not stored
    refresh  ignore what is stored, fetch, and overwrite it
    off      no cache at all

The key covers everything that can change an answer — method, URL, body and the
browser-agent switch — so a page fetched as a browser is not served back to a
request that asked as the research agent.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

LOGGER = logging.getLogger("catalogue-dump.cache")

MODES = ("off", "auto", "replay", "refresh")


@dataclass
class CachedResponse:
    status: int
    url: str
    body: str
    headers: dict[str, str]
    fetched_at: float
    kind: str = "http"


class ResponseCache:
    """Stores one gzipped JSON document per request under a sharded path."""

    def __init__(self, directory: Path | str, mode: str = "auto", max_age: float | None = None) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown cache mode {mode!r}; expected one of {', '.join(MODES)}")
        self.directory = Path(directory)
        self.mode = mode
        #: Seconds a stored response stays usable in `auto`; None means forever.
        self.max_age = max_age
        self.hits = 0
        self.misses = 0
        self.writes = 0

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def key(self, kind: str, url: str, **parts: Any) -> str:
        material = json.dumps([kind, url, parts], sort_keys=True, default=str)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def path(self, key: str, url: str = "") -> Path:
        # Shard by the first bytes of the digest so no directory grows to
        # hundreds of thousands of entries, and keep the host in the path so a
        # human can see what a cache holds and drop one site's pages by hand.
        host = urlparse(url).netloc or "other"
        return self.directory / host / key[:2] / f"{key}.json.gz"

    def read(self, key: str, url: str = "") -> CachedResponse | None:
        if not self.enabled or self.mode == "refresh":
            return None
        path = self.path(key, url)
        if not path.exists():
            self.misses += 1
            return None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                stored = json.load(handle)
        except (OSError, json.JSONDecodeError, EOFError) as error:
            LOGGER.warning("unreadable cache entry %s (%s); refetching", path, error)
            self.misses += 1
            return None
        entry = CachedResponse(**stored)
        if self.max_age is not None and time.time() - entry.fetched_at > self.max_age:
            self.misses += 1
            return None
        self.hits += 1
        return entry

    def write(self, key: str, entry: CachedResponse) -> None:
        if not self.enabled:
            return
        path = self.path(key, entry.url)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written beside the target and moved into place, so a cancelled run
        # never leaves a half-written entry that a later replay would trust.
        temporary = path.with_suffix(".tmp")
        try:
            with gzip.open(temporary, "wt", encoding="utf-8") as handle:
                json.dump(entry.__dict__, handle)
            temporary.replace(path)
            self.writes += 1
        except OSError as error:  # pragma: no cover - disk-dependent
            LOGGER.warning("could not cache %s (%s)", entry.url, error)
            temporary.unlink(missing_ok=True)

    def summary(self) -> str:
        total = self.hits + self.misses
        share = (self.hits / total * 100) if total else 0.0
        return f"cache mode={self.mode} hits={self.hits} ({share:.0f}%) misses={self.misses} stored={self.writes}"
