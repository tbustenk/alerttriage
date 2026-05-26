"""Security middleware for the AlertTriage API.

Adds HTTP security headers and enforces a maximum request body size.
Written as pure ASGI middleware (not BaseHTTPMiddleware) for compatibility
with Python 3.11+ exception groups and anyio task groups.
"""

from __future__ import annotations

import os
from typing import Any

_HEADERS: list[tuple[bytes, bytes]] = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"x-xss-protection", b"1; mode=block"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"geolocation=(), microphone=(), camera=()"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none';"),
]

_MAX_BODY_BYTES: int = int(os.environ.get("ALERTTRIAGE_MAX_BODY_BYTES", str(1 * 1024 * 1024)))  # 1 MiB


class SecurityHeadersMiddleware:
    """Attach security headers to every HTTP response.

    Pure ASGI implementation — no BaseHTTPMiddleware dependency.
    HSTS is added when ``ALERTTRIAGE_ENABLE_HSTS=1``.
    """

    _hsts: tuple[bytes, bytes] = (
        b"strict-transport-security",
        b"max-age=31536000; includeSubDomains; preload",
    )

    def __init__(self, app: Any) -> None:
        self._app = app
        self._enable_hsts = os.environ.get("ALERTTRIAGE_ENABLE_HSTS", "").lower() in ("1", "true")

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def _send(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers: list[tuple[bytes, bytes]] = list(message.get("headers", []))
                headers.extend(_HEADERS)
                if self._enable_hsts:
                    headers.append(self._hsts)
                message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, _send)


class RequestSizeLimitMiddleware:
    """Reject requests whose Content-Length exceeds *max_bytes*.

    Pure ASGI implementation.
    """

    def __init__(self, app: Any, *, max_bytes: int = _MAX_BODY_BYTES) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        for header_name, header_value in scope.get("headers", []):
            if header_name == b"content-length":
                try:
                    if int(header_value) > self._max_bytes:
                        await _send_413(send, self._max_bytes)
                        return
                except ValueError:
                    pass

        await self._app(scope, receive, send)


async def _send_413(send: Any, max_bytes: int) -> None:
    import json

    body = json.dumps(
        {"detail": f"Request body too large. Maximum: {max_bytes} bytes."}
    ).encode()
    await send({
        "type": "http.response.start",
        "status": 413,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body, "more_body": False})
