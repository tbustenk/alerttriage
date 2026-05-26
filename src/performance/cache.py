"""Thread-safe LRU cache with TTL for AlertTriage.

Provides a simple in-process cache to avoid redundant DB queries or
expensive computations for frequently requested data.

Usage::

    from alerttriage.src.performance.cache import TTLCache

    _stats_cache = TTLCache(maxsize=256, ttl_seconds=30)

    def get_stats(client_id: str) -> dict:
        hit = _stats_cache.get(f"stats:{client_id}")
        if hit is not None:
            return hit
        result = _compute_stats(client_id)
        _stats_cache.set(f"stats:{client_id}", result)
        return result
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any


class TTLCache:
    """Thread-safe LRU cache with per-entry time-to-live.

    Args:
        maxsize:     Maximum number of entries before LRU eviction.
        ttl_seconds: How long each entry lives before it is considered stale.
    """

    def __init__(self, maxsize: int = 256, ttl_seconds: float = 60.0) -> None:
        self._maxsize = maxsize
        self._ttl = ttl_seconds
        self._cache: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """Return the cached value for *key*, or *default* if missing / expired."""
        with self._lock:
            if key not in self._cache:
                return default
            value, expires_at = self._cache[key]
            if time.monotonic() > expires_at:
                del self._cache[key]
                return default
            self._cache.move_to_end(key)
            return value

    def set(self, key: str, value: Any) -> None:
        """Store *value* under *key*, evicting the LRU entry if needed."""
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = (value, time.monotonic() + self._ttl)
            if len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)

    def delete(self, key: str) -> None:
        """Remove a single entry (noop if absent)."""
        with self._lock:
            self._cache.pop(key, None)

    def delete_prefix(self, prefix: str) -> int:
        """Remove all entries whose key starts with *prefix*. Returns count removed."""
        with self._lock:
            to_remove = [k for k in self._cache if k.startswith(prefix)]
            for k in to_remove:
                del self._cache[k]
            return len(to_remove)

    def clear(self) -> None:
        """Evict all entries."""
        with self._lock:
            self._cache.clear()

    def __len__(self) -> int:
        return len(self._cache)

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None


# ---------------------------------------------------------------------------
# Module-level shared caches
# ---------------------------------------------------------------------------

#: Short-lived cache for per-client stats (30 s TTL).
stats_cache: TTLCache = TTLCache(maxsize=512, ttl_seconds=30.0)

#: Medium-lived cache for config lookups (60 s TTL).
config_cache: TTLCache = TTLCache(maxsize=256, ttl_seconds=60.0)

#: Long-lived cache for analytics summaries (5 min TTL).
analytics_cache: TTLCache = TTLCache(maxsize=128, ttl_seconds=300.0)
