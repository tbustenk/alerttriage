"""Sliding-window in-memory rate limiter for the AlertTriage REST API.

No external dependency (Redis / memcached) required.  Limit is per source IP
(or the first address in X-Forwarded-For when behind a proxy).

Configured via:  ALERTTRIAGE_API_RATE_LIMIT=60   (requests per minute)
"""

from __future__ import annotations

import os
import time
from collections import deque
from threading import Lock

from fastapi import HTTPException, Request, status


class _Bucket:
    __slots__ = ("timestamps", "lock")

    def __init__(self) -> None:
        self.timestamps: deque[float] = deque()
        self.lock = Lock()


class RateLimiter:
    """Sliding 60-second window rate limiter."""

    def __init__(self, *, requests_per_minute: int = 60, rpm: int | None = None) -> None:
        if rpm is not None:
            requests_per_minute = rpm
        self._rpm = requests_per_minute
        self._buckets: dict[str, _Bucket] = {}
        self._global_lock = Lock()

    def check(self, key: str) -> None:
        """Raise HTTP 429 if ``key`` has exhausted its per-minute quota."""
        bucket = self._get_bucket(key)
        now = time.monotonic()
        cutoff = now - 60.0

        with bucket.lock:
            while bucket.timestamps and bucket.timestamps[0] < cutoff:
                bucket.timestamps.popleft()
            if len(bucket.timestamps) >= self._rpm:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Rate limit exceeded: {self._rpm} requests/minute allowed",
                    headers={"Retry-After": "60"},
                )
            bucket.timestamps.append(now)

    def _get_bucket(self, key: str) -> _Bucket:
        with self._global_lock:
            if key not in self._buckets:
                self._buckets[key] = _Bucket()
            return self._buckets[key]


def _rpm_from_env() -> int:
    try:
        return int(os.environ.get("ALERTTRIAGE_API_RATE_LIMIT", "60"))
    except ValueError:
        return 60


limiter = RateLimiter(requests_per_minute=_rpm_from_env())


def request_key(request: Request) -> str:
    """Return a per-client key for rate limiting (IP or forwarded IP)."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"
