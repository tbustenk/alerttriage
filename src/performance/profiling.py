"""Request-timing middleware and profiling utilities.

Pure ASGI implementation for compatibility with Python 3.11+ exception groups.
Tracks per-route latency and logs requests that exceed a configurable threshold.
"""

from __future__ import annotations

import time
from collections import deque
from threading import RLock
from typing import Any

from alerttriage.src.logger import get_logger

log = get_logger(__name__)

_DEFAULT_SLOW_MS = float(500)


class TimingMiddleware:
    """Measures per-request wall-clock latency and logs slow requests.

    Pure ASGI implementation.

    Args:
        slow_threshold_ms: Requests taking longer than this are logged as warnings.
    """

    def __init__(self, app: Any, *, slow_threshold_ms: float = _DEFAULT_SLOW_MS) -> None:
        self._app = app
        self._threshold = slow_threshold_ms
        self._samples: deque[tuple[str, float]] = deque(maxlen=2000)
        self._lock = RLock()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        start = time.monotonic()
        await self._app(scope, receive, send)
        latency_ms = (time.monotonic() - start) * 1000

        method = scope.get("method", "")
        path = scope.get("path", "")
        route_key = f"{method} {path}"

        with self._lock:
            self._samples.append((route_key, latency_ms))

        if latency_ms > self._threshold:
            log.warning(
                "slow_request",
                route=route_key,
                latency_ms=round(latency_ms, 1),
                threshold_ms=self._threshold,
            )

    def percentile(self, route_prefix: str = "", pct: float = 95.0) -> float | None:
        """Return the *pct*-th percentile latency for routes matching *route_prefix*."""
        with self._lock:
            times = [t for r, t in self._samples if r.startswith(route_prefix)]
        if not times:
            return None
        times.sort()
        idx = min(int(len(times) * pct / 100), len(times) - 1)
        return times[idx]

    def summary(self) -> dict[str, Any]:
        """Return a snapshot of latency percentiles for all captured samples."""
        with self._lock:
            if not self._samples:
                return {"samples": 0}
            times = [t for _, t in self._samples]
        times.sort()
        n = len(times)

        def _pct(p: float) -> float:
            return round(times[min(int(n * p / 100), n - 1)], 1)

        return {
            "samples": n,
            "p50_ms": _pct(50),
            "p95_ms": _pct(95),
            "p99_ms": _pct(99),
            "max_ms": round(times[-1], 1),
            "min_ms": round(times[0], 1),
        }
