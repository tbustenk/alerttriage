"""Webhook delivery: HMAC signing, async POST, exponential-backoff retry."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from alerttriage.src.logger import get_logger
from alerttriage.src.webhooks.events import DeliveryResult, WebhookEvent, WebhookRegistration

log = get_logger(__name__)

_USER_AGENT = "AlertTriage-Webhook/2.0"


def _sign_payload(payload_bytes: bytes, secret: str) -> str:
    """Return ``sha256=<hex>`` HMAC signature."""
    sig = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


async def deliver(
    registration: WebhookRegistration,
    event: WebhookEvent,
    *,
    max_attempts: int = 4,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
    timeout: float = 10.0,
) -> DeliveryResult:
    """Deliver *event* to *registration.url* with exponential-backoff retry.

    Retry schedule (seconds): 1, 2, 4, 8 (capped at *max_delay*).
    4xx responses are NOT retried (client error — retrying won't help).
    5xx and network errors are retried up to *max_attempts* total.
    """
    payload_bytes = event.model_dump_json().encode("utf-8")
    headers: dict[str, str] = {
        "Content-Type":   "application/json",
        "User-Agent":     _USER_AGENT,
        "X-AlertTriage-Event":  event.event_type,
        "X-AlertTriage-Event-Id": event.id,
        "X-AlertTriage-Timestamp": event.timestamp.isoformat(),
        "X-AlertTriage-Client": event.client_id,
    }
    if registration.secret:
        headers["X-AlertTriage-Signature"] = _sign_payload(payload_bytes, registration.secret)

    last_error: str | None = None
    last_status: int | None = None
    last_attempt: int = 0

    async with httpx.AsyncClient() as client:
        for attempt in range(1, max_attempts + 1):
            last_attempt = attempt
            try:
                resp = await client.post(
                    registration.url,
                    content=payload_bytes,
                    headers=headers,
                    timeout=timeout,
                )
                last_status = resp.status_code

                if resp.status_code < 300:
                    log.info(
                        "webhook_delivered",
                        webhook_id=registration.id,
                        event_type=event.event_type,
                        url=registration.url,
                        status=resp.status_code,
                        attempts=attempt,
                    )
                    return DeliveryResult(
                        webhook_id=registration.id,
                        event_id=event.id,
                        success=True,
                        status_code=resp.status_code,
                        attempts=attempt,
                    )

                if 400 <= resp.status_code < 500:
                    last_error = f"HTTP {resp.status_code} (non-retryable)"
                    log.warning(
                        "webhook_client_error",
                        webhook_id=registration.id,
                        status=resp.status_code,
                    )
                    break   # client error — stop retrying

                last_error = f"HTTP {resp.status_code}"

            except httpx.TimeoutException:
                last_error = f"Timeout after {timeout}s"
            except httpx.RequestError as exc:
                last_error = str(exc)

            log.warning(
                "webhook_attempt_failed",
                webhook_id=registration.id,
                attempt=attempt,
                error=last_error,
            )

            if attempt < max_attempts:
                delay = min(initial_delay * (2 ** (attempt - 1)), max_delay)
                await asyncio.sleep(delay)

    log.error(
        "webhook_delivery_failed",
        webhook_id=registration.id,
        url=registration.url,
        event_type=event.event_type,
        error=last_error,
    )
    return DeliveryResult(
        webhook_id=registration.id,
        event_id=event.id,
        success=False,
        status_code=last_status,
        attempts=last_attempt,
        error=last_error,
    )


def verify_signature(payload_bytes: bytes, signature_header: str, secret: str) -> bool:
    """Return True if *signature_header* matches the expected HMAC.

    Callers (SOAR systems, reverse-proxy checks) use this to verify that
    an inbound AlertTriage payload is authentic.
    """
    if not signature_header.startswith("sha256="):
        return False
    expected = _sign_payload(payload_bytes, secret)
    return hmac.compare_digest(signature_header, expected)
