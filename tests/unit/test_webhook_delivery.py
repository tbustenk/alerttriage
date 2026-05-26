"""Unit tests for webhook delivery — signing, retry, and result handling."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alerttriage.src.webhooks.delivery import deliver, verify_signature
from alerttriage.src.webhooks.events import EventType, WebhookEvent, WebhookRegistration


def _make_registration(*, url: str = "https://example.com/hook", secret: str | None = None):
    return WebhookRegistration(
        id="wh-1",
        client_id="test-client",
        url=url,
        events=["alert_analyzed"],
        secret=secret,
    )


def _make_event():
    return WebhookEvent(
        event_type=EventType.ALERT_ANALYZED,
        client_id="test-client",
        data={"alert_id": "a-1", "verdict": "true_positive", "confidence": 0.9},
    )


class TestVerifySignature:
    def test_valid_signature(self):
        secret = "test-secret"
        payload = b'{"foo": "bar"}'
        sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        assert verify_signature(payload, sig, secret) is True

    def test_invalid_signature(self):
        secret = "correct-secret"
        payload = b'{"foo": "bar"}'
        bad_sig = "sha256=deadbeef" + "0" * 50
        assert verify_signature(payload, bad_sig, secret) is False

    def test_wrong_prefix(self):
        secret = "secret"
        payload = b"data"
        plain_hex = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        assert verify_signature(payload, plain_hex, secret) is False

    def test_empty_payload(self):
        secret = "secret"
        payload = b""
        sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        assert verify_signature(payload, sig, secret) is True


class TestDelivery:
    def test_successful_delivery(self):
        registration = _make_registration()
        event = _make_event()

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("alerttriage.src.webhooks.delivery.httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(deliver(registration, event))

        assert result.success is True
        assert result.status_code == 200
        assert result.attempts == 1
        assert result.error is None

    def test_delivery_with_hmac_signature(self):
        registration = _make_registration(secret="my-secret")
        event = _make_event()

        captured_headers = {}
        mock_response = MagicMock()
        mock_response.status_code = 200

        async def _fake_post(url, *, content, headers, timeout):
            captured_headers.update(headers)
            return mock_response

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = _fake_post

        with patch("alerttriage.src.webhooks.delivery.httpx.AsyncClient", return_value=mock_client):
            asyncio.run(deliver(registration, event))

        assert "X-AlertTriage-Signature" in captured_headers
        sig_header = captured_headers["X-AlertTriage-Signature"]
        assert sig_header.startswith("sha256=")

    def test_4xx_non_retryable(self):
        registration = _make_registration()
        event = _make_event()

        mock_response = MagicMock()
        mock_response.status_code = 404

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("alerttriage.src.webhooks.delivery.httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(deliver(registration, event, max_attempts=4))

        assert result.success is False
        # 4xx is non-retryable — should stop immediately
        assert result.attempts == 1

    def test_5xx_retries_and_fails(self):
        registration = _make_registration()
        event = _make_event()

        mock_response = MagicMock()
        mock_response.status_code = 503

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("alerttriage.src.webhooks.delivery.httpx.AsyncClient", return_value=mock_client):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = asyncio.run(deliver(registration, event, max_attempts=2, initial_delay=0.001))

        assert result.success is False
        assert result.attempts == 2  # tried twice

    def test_network_error_retries(self):
        import httpx

        registration = _make_registration()
        event = _make_event()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))

        with patch("alerttriage.src.webhooks.delivery.httpx.AsyncClient", return_value=mock_client):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = asyncio.run(deliver(registration, event, max_attempts=2, initial_delay=0.001))

        assert result.success is False
        assert result.attempts == 2
        assert result.error is not None

    def test_timeout_error_retries(self):
        import httpx

        registration = _make_registration()
        event = _make_event()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))

        with patch("alerttriage.src.webhooks.delivery.httpx.AsyncClient", return_value=mock_client):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = asyncio.run(deliver(registration, event, max_attempts=2, initial_delay=0.001))

        assert result.success is False
        assert "Timeout" in result.error
