"""Audit logging middleware — records every API request with actor and outcome.

Pure ASGI implementation for compatibility with Python 3.11+ exception groups.
Logs are emitted via structlog, integrating with the existing log pipeline.
"""

from __future__ import annotations

import time
from typing import Any

from alerttriage.src.logger import get_logger

log = get_logger("alerttriage.audit")

_SKIP_PATHS: frozenset[bytes] = frozenset({b"/metrics", b"/health", b"/favicon.ico"})
_KEY_HINT_LEN = 6


class AuditMiddleware:
    """Structured audit trail for every authenticated API request.

    Pure ASGI implementation — no BaseHTTPMiddleware dependency.

    Each log entry contains:
    - ``method`` / ``path``      — what was called
    - ``status``                 — HTTP response code
    - ``latency_ms``             — wall-clock time
    - ``ip``                     — client IP (X-Forwarded-For aware)
    - ``api_key_hint``           — first few chars of the key
    """

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path: bytes = scope.get("path", "").encode() if isinstance(scope.get("path"), str) else scope.get("path", b"")
        if path in _SKIP_PATHS:
            await self._app(scope, receive, send)
            return

        start = time.monotonic()
        status_code: list[int] = [0]

        async def _send(message: dict) -> None:
            if message["type"] == "http.response.start":
                status_code[0] = message.get("status", 0)
            await send(message)

        await self._app(scope, receive, _send)

        latency_ms = (time.monotonic() - start) * 1000
        headers: dict[bytes, bytes] = dict(scope.get("headers", []))
        api_key = (headers.get(b"x-api-key") or b"").decode()
        key_hint = api_key[:_KEY_HINT_LEN] + "…" if len(api_key) > _KEY_HINT_LEN else "anonymous"

        log.info(
            "api_request",
            method=scope.get("method", ""),
            path=scope.get("path", ""),
            status=status_code[0],
            latency_ms=round(latency_ms, 1),
            ip=_client_ip(scope),
            api_key_hint=key_hint,
        )


def _client_ip(scope: dict) -> str:
    headers: dict[bytes, bytes] = dict(scope.get("headers", []))
    forwarded = headers.get(b"x-forwarded-for")
    if forwarded:
        return forwarded.decode().split(",")[0].strip()
    client = scope.get("client")
    if client:
        return client[0]
    return "unknown"
