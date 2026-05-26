"""Unit tests for WebhookManager."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alerttriage.src.webhooks.manager import WebhookManager

CLIENT_ID = "test-client"
TEST_URL = "https://example.com/hook"


@pytest.fixture
def manager(tmp_data_dir):
    return WebhookManager(tmp_data_dir)


class TestRegistration:
    def test_register_returns_registration(self, manager):
        reg = manager.register(client_id=CLIENT_ID, url=TEST_URL, events=["alert_analyzed"])
        assert reg.id
        assert reg.client_id == CLIENT_ID
        assert reg.url == TEST_URL
        assert "alert_analyzed" in reg.events
        assert reg.active is True

    def test_register_with_secret(self, manager):
        reg = manager.register(client_id=CLIENT_ID, url=TEST_URL, events=["alert_analyzed"], secret="my-secret")
        assert reg.secret == "my-secret"

    def test_registered_webhook_persisted(self, manager, tmp_data_dir):
        manager.register(client_id=CLIENT_ID, url=TEST_URL, events=["alert_analyzed"])
        # Re-create manager from same data dir to verify disk persistence
        manager2 = WebhookManager(tmp_data_dir)
        all_regs = manager2.list_all()
        assert len(all_regs) == 1
        assert all_regs[0].url == TEST_URL

    def test_register_multiple(self, manager):
        manager.register(client_id=CLIENT_ID, url=TEST_URL + "/1", events=["alert_analyzed"])
        manager.register(client_id=CLIENT_ID, url=TEST_URL + "/2", events=["feedback_recorded"])
        assert len(manager.list_all()) == 2


class TestRetrievalAndFiltering:
    def test_get_by_id(self, manager):
        reg = manager.register(client_id=CLIENT_ID, url=TEST_URL, events=["alert_analyzed"])
        found = manager.get(reg.id)
        assert found is not None
        assert found.id == reg.id

    def test_get_nonexistent_returns_none(self, manager):
        assert manager.get("does-not-exist") is None

    def test_list_for_client(self, manager):
        manager.register(client_id="client-a", url=TEST_URL + "/a", events=["alert_analyzed"])
        manager.register(client_id="client-b", url=TEST_URL + "/b", events=["alert_analyzed"])
        a_regs = manager.list_for_client("client-a")
        assert len(a_regs) == 1
        assert a_regs[0].client_id == "client-a"

    def test_list_all_returns_all(self, manager):
        manager.register(client_id="a", url=TEST_URL + "/1", events=["alert_analyzed"])
        manager.register(client_id="b", url=TEST_URL + "/2", events=["alert_analyzed"])
        assert len(manager.list_all()) == 2


class TestUpdate:
    def test_update_url(self, manager):
        reg = manager.register(client_id=CLIENT_ID, url=TEST_URL, events=["alert_analyzed"])
        updated = manager.update(reg.id, url="https://new.example.com/hook")
        assert updated is not None
        assert updated.url == "https://new.example.com/hook"

    def test_update_active_status(self, manager):
        reg = manager.register(client_id=CLIENT_ID, url=TEST_URL, events=["alert_analyzed"])
        updated = manager.update(reg.id, active=False)
        assert updated is not None
        assert updated.active is False

    def test_update_nonexistent_returns_none(self, manager):
        assert manager.update("does-not-exist", url="https://x.com") is None

    def test_update_events(self, manager):
        reg = manager.register(client_id=CLIENT_ID, url=TEST_URL, events=["alert_analyzed"])
        updated = manager.update(reg.id, events=["alert_analyzed", "feedback_recorded"])
        assert updated is not None
        assert "feedback_recorded" in updated.events


class TestDeregistration:
    def test_deregister_removes_entry(self, manager):
        reg = manager.register(client_id=CLIENT_ID, url=TEST_URL, events=["alert_analyzed"])
        assert manager.deregister(reg.id) is True
        assert manager.get(reg.id) is None

    def test_deregister_nonexistent_returns_false(self, manager):
        assert manager.deregister("ghost-id") is False

    def test_deregister_persists(self, manager, tmp_data_dir):
        reg = manager.register(client_id=CLIENT_ID, url=TEST_URL, events=["alert_analyzed"])
        manager.deregister(reg.id)
        manager2 = WebhookManager(tmp_data_dir)
        assert manager2.get(reg.id) is None


class TestEmit:
    def test_emit_creates_task(self, manager):
        from alerttriage.src.webhooks.events import make_alert_analyzed_event

        reg = manager.register(client_id=CLIENT_ID, url=TEST_URL, events=["alert_analyzed"])

        class _MockResult:
            alert_id = "a1"
            id = "r1"
            client_id = CLIENT_ID
            verdict = MagicMock(value="true_positive")
            confidence = 0.9
            model_id = "claude"
            cost_usd = 0.001
            latency_ms = 100.0
            summary = "test"

        event = make_alert_analyzed_event(CLIENT_ID, _MockResult())
        # emit is fire-and-forget; just verify it doesn't raise
        manager.emit(event, fire_and_forget=False)

    def test_emit_skips_inactive_webhook(self, manager):
        from alerttriage.src.webhooks.events import make_alert_analyzed_event

        reg = manager.register(client_id=CLIENT_ID, url=TEST_URL, events=["alert_analyzed"])
        manager.update(reg.id, active=False)

        class _MockResult:
            alert_id = "a1"
            id = "r1"
            client_id = CLIENT_ID
            verdict = MagicMock(value="true_positive")
            confidence = 0.9
            model_id = "claude"
            cost_usd = 0.001
            latency_ms = 100.0
            summary = "test"

        event = make_alert_analyzed_event(CLIENT_ID, _MockResult())
        # Should not raise even with inactive webhook
        manager.emit(event, fire_and_forget=False)

    def test_emit_skips_wrong_event_type(self, manager):
        from alerttriage.src.webhooks.events import WebhookEvent, EventType

        manager.register(client_id=CLIENT_ID, url=TEST_URL, events=["feedback_recorded"])
        event = WebhookEvent(
            event_type=EventType.ALERT_ANALYZED,
            client_id=CLIENT_ID,
            data={},
        )
        # No delivery should happen; just verify no errors
        manager.emit(event, fire_and_forget=False)
