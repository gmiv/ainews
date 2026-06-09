"""Tiny JSON file cache with per-entry TTL.

Used to (a) avoid re-fetching RSS feeds on quick relaunches and (b) avoid
re-billing identical LLM calls. Values must be JSON-serializable; failures are
swallowed so the cache can never break the app.
"""
import hashlib
import json
import os
import time

from . import config


class Cache:
    def __init__(self, directory=None, enabled=None):
        self.directory = directory or config.CACHE_DIR
        self.enabled = config.ENABLE_CACHE if enabled is None else enabled
        if self.enabled:
            try:
                os.makedirs(self.directory, exist_ok=True)
            except OSError:
                self.enabled = False

    def _path(self, key: str) -> str:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return os.path.join(self.directory, digest + ".json")

    def get(self, key: str, ttl):
        """Return the cached value, or None if missing/expired/unreadable."""
        if not self.enabled:
            return None
        try:
            with open(self._path(key), "r", encoding="utf-8") as fh:
                blob = json.load(fh)
        except (OSError, ValueError):
            return None
        if ttl is not None and (time.time() - blob.get("ts", 0)) > ttl:
            return None
        return blob.get("value")

    def set(self, key: str, value) -> None:
        if not self.enabled:
            return
        try:
            with open(self._path(key), "w", encoding="utf-8") as fh:
                json.dump({"ts": time.time(), "value": value}, fh)
        except (OSError, TypeError):
            pass


def make_key(*parts) -> str:
    """Build a cache key from arbitrary parts."""
    return "|".join(str(p) for p in parts)
