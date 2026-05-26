"""Unit tests for WebhookManager.emit() and test_delivery()."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

from alerttriage.src.webhooks.events import EventType, WebhookEvent
from alerttriage.src.webhooks.manager import WebhookManager


def _manager(tmp: Path) -> WebhookManager:
    return WebhookManager(tmp)


def _make_event(client_id: str = "acme"):
    return WebhookEvent(
        event_type=EventType.ALERT_ANALYZED,
        client_id=client_id,
        data={"alert_id": "a-1", "verdict": "true_positive"},
    )


class TestEmitAndDeliver:
    def test_emit_fire_and_forget_no_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _manager(Path(tmp))
            event = _make_event()
            results = asyncio.run(mgr.emit(event, fire_and_forget=False))
            assert results == []

    def test_emit_delivers_to_matching_webhook(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _manager(Path(tmp))
            mgr.register("acme", "https://hook.example.com", ["alert_analyzed"])
            event = _make_event("acme")

            mock_result = MagicMock()
            mock_result.success = True
            mock_result.status_code = 200

            with patch("alerttriage.src.webhooks.manager.deliver", new_callable=AsyncMock, return_value=mock_result):
                results = asyncio.run(mgr.emit(event, fire_and_forget=False))

            assert len(results) == 1
            assert results[0].success is True

    def test_emit_skips_inactive_webhook(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _manager(Path(tmp))
            reg = mgr.register("acme", "https://hook.example.com", ["alert_analyzed"])
            mgr.update(reg.id, active=False)

            event = _make_event("acme")
            results = asyncio.run(mgr.emit(event, fire_and_forget=False))
            assert results == []

    def test_emit_skips_wrong_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _manager(Path(tmp))
            mgr.register("other-client", "https://hook.example.com", ["alert_analyzed"])
            event = _make_event("acme")
            results = asyncio.run(mgr.emit(event, fire_and_forget=False))
            assert results == []

    def test_emit_updates_last_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _manager(Path(tmp))
            reg = mgr.register("acme", "https://hook.example.com", ["alert_analyzed"])
            event = _make_event("acme")

            mock_result = MagicMock()
            mock_result.success = True
            mock_result.status_code = 200

            with patch("alerttriage.src.webhooks.manager.deliver", new_callable=AsyncMock, return_value=mock_result):
                asyncio.run(mgr.emit(event, fire_and_forget=False))

            updated = mgr.get(reg.id)
            assert updated.last_success_at is not None
            assert updated.consecutive_failures == 0

    def test_emit_increments_failure_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _manager(Path(tmp))
            reg = mgr.register("acme", "https://hook.example.com", ["alert_analyzed"])
            event = _make_event("acme")

            mock_result = MagicMock()
            mock_result.success = False
            mock_result.status_code = 503
            mock_result.error = "server error"

            with patch("alerttriage.src.webhooks.manager.deliver", new_callable=AsyncMock, return_value=mock_result):
                asyncio.run(mgr.emit(event, fire_and_forget=False))

            updated = mgr.get(reg.id)
            assert updated.consecutive_failures == 1
            assert updated.active is True  # not yet disabled

    def test_emit_fire_and_forget_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _manager(Path(tmp))
            mgr.register("acme", "https://hook.example.com", ["alert_analyzed"])
            event = _make_event("acme")

            mock_result = MagicMock()
            mock_result.success = True

            with patch("alerttriage.src.webhooks.manager.deliver", new_callable=AsyncMock, return_value=mock_result):
                results = asyncio.run(mgr.emit(event, fire_and_forget=True))
            # fire-and-forget returns empty list immediately
            assert results == []


class TestTestDelivery:
    def test_test_delivery_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _manager(Path(tmp))
            result = asyncio.run(mgr.test_delivery("nonexistent-id"))
            assert result.success is False
            assert result.error == "Webhook not found"

    def test_test_delivery_sends_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _manager(Path(tmp))
            reg = mgr.register("acme", "https://hook.example.com", ["alert_analyzed"])

            mock_result = MagicMock()
            mock_result.success = True
            mock_result.status_code = 200

            with patch("alerttriage.src.webhooks.manager.deliver", new_callable=AsyncMock, return_value=mock_result):
                result = asyncio.run(mgr.test_delivery(reg.id))

            assert result.success is True
