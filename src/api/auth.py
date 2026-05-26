"""API key authentication for the AlertTriage REST API.

Keys are loaded from the ALERTTRIAGE_API_KEYS environment variable as a
comma-separated list.  If the variable is empty, the API runs in open mode
with a startup warning — useful for local development.

Header:  X-API-Key: <key>
"""

from __future__ import annotations

import os
import secrets

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

_HEADER_SCHEME = APIKeyHeader(name="X-API-Key", auto_error=False)


def _load_valid_keys() -> frozenset[str]:
    raw = os.environ.get("ALERTTRIAGE_API_KEYS", "")
    return frozenset(k.strip() for k in raw.split(",") if k.strip())


# Evaluated once at import time; restart the process to pick up new keys.
VALID_KEYS: frozenset[str] = _load_valid_keys()


async def require_api_key(
    api_key: str | None = Security(_HEADER_SCHEME),
) -> str:
    """FastAPI dependency — validates ``X-API-Key``.

    Returns the key on success so handlers can log which key was used.
    Returns ``"anonymous"`` when no keys are configured (open-access mode).
    """
    if not VALID_KEYS:
        return "anonymous"

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    # Constant-time comparison to prevent timing attacks.
    matched = any(secrets.compare_digest(api_key, valid) for valid in VALID_KEYS)
    if not matched:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )
    return api_key
