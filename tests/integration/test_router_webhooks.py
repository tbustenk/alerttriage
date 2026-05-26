"""Integration tests for the /webhooks router."""

from __future__ import annotations

import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from tests.conftest import CLIENT_ID, _FakeConfig


@pytest.fixture
def webhook_client(monkeypatch):
    """TestClient with a real WebhookManager injected."""
    import alerttriage.src.api.app as _app
    import alerttriage.src.api.auth as _auth
    from alerttriage.src.api.app import app
    from alerttriage.src.core.analyzer import AlertAnalyzer
    from alerttriage.src.monitoring.health_monitor import HealthMonitor
    from alerttriage.src.webhooks.manager import WebhookManager
    from fastapi.testclient import TestClient

    monkeypatch.setattr(_auth, "VALID_KEYS", frozenset(["test-key"]))

    with tempfile.TemporaryDirectory() as tmp:
        tmp_data = Path(tmp) / "data"
        tmp_data.mkdir()

        @asynccontextmanager
        async def _lifespan(_):
            config = _FakeConfig(tmp_data)
            _app._analyzer = AlertAnalyzer(config)
            _app._monitor = HealthMonitor(config)
            _app._feedback_systems = {}
            _app._webhook_manager = WebhookManager(tmp_data)
            _app._analytics_engine = None
            _app._report_scheduler = None
            _app._config_versioning = None
            _app._feature_flags = None
            _app._alert_type_settings = None
            _app._reports_dir = tmp_data / "reports"
            yield
            _app._analyzer = None
            _app._monitor = None
            _app._feedback_systems = {}
            _app._webhook_manager = None

        original = app.router.lifespan_context
        app.router.lifespan_context = _lifespan
        try:
            with TestClient(app, raise_server_exceptions=True) as client:
                client.headers.update({"X-API-Key": "test-key"})
                yield client
        finally:
            app.router.lifespan_context = original
            _app._analyzer = None
            _app._monitor = None
            _app._feedback_systems = {}
            _app._webhook_manager = None


class TestEventTypes:
    def test_list_event_types(self, webhook_client):
        resp = webhook_client.get("/webhooks/event-types")
        assert resp.status_code == 200
        types = resp.json()
        assert isinstance(types, list)
        assert "alert_analyzed" in types
        assert "feedback_recorded" in types

    def test_no_auth_required(self, webhook_client):
        # event-types has no auth dependency
        resp = webhook_client.get("/webhooks/event-types", headers={"X-API-Key": ""})
        assert resp.status_code == 200


class TestRegister:
    def test_register_valid(self, webhook_client):
        resp = webhook_client.post("/webhooks", json={
            "client_id": CLIENT_ID,
            "url": "https://hook.example.com/endpoint",
            "events": ["alert_analyzed"],
        })
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert data["client_id"] == CLIENT_ID
        assert data["url"] == "https://hook.example.com/endpoint"
        assert data["active"] is True
        assert data["has_secret"] is False

    def test_register_with_secret(self, webhook_client):
        resp = webhook_client.post("/webhooks", json={
            "client_id": CLIENT_ID,
            "url": "https://hook.example.com/secure",
            "events": ["feedback_recorded"],
            "secret": "my-signing-secret",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["has_secret"] is True

    def test_register_invalid_event(self, webhook_client):
        resp = webhook_client.post("/webhooks", json={
            "client_id": CLIENT_ID,
            "url": "https://hook.example.com/x",
            "events": ["not_a_real_event"],
        })
        assert resp.status_code == 422

    def test_register_no_auth(self, webhook_client):
        resp = webhook_client.post(
            "/webhooks",
            json={"client_id": CLIENT_ID, "url": "https://x.com", "events": ["alert_analyzed"]},
            headers={"X-API-Key": "bad-key"},
        )
        assert resp.status_code == 403

    def test_register_multiple_events(self, webhook_client):
        resp = webhook_client.post("/webhooks", json={
            "client_id": CLIENT_ID,
            "url": "https://hook.example.com/multi",
            "events": ["alert_analyzed", "feedback_recorded"],
            "description": "Multi-event hook",
        })
        assert resp.status_code == 201
        assert resp.json()["description"] == "Multi-event hook"


class TestList:
    def _register(self, client, url="https://example.com/hook"):
        return client.post("/webhooks", json={
            "client_id": CLIENT_ID,
            "url": url,
            "events": ["alert_analyzed"],
        }).json()

    def test_list_empty(self, webhook_client):
        resp = webhook_client.get("/webhooks")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_after_register(self, webhook_client):
        self._register(webhook_client)
        resp = webhook_client.get("/webhooks")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_list_filtered_by_client(self, webhook_client):
        self._register(webhook_client)
        resp = webhook_client.get(f"/webhooks?client_id={CLIENT_ID}")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_list_filtered_no_match(self, webhook_client):
        self._register(webhook_client)
        resp = webhook_client.get("/webhooks?client_id=other-client")
        assert resp.status_code == 200
        assert resp.json() == []


class TestGetUpdateDelete:
    def _register(self, client):
        return client.post("/webhooks", json={
            "client_id": CLIENT_ID,
            "url": "https://example.com/hook",
            "events": ["alert_analyzed"],
        }).json()

    def test_get_existing(self, webhook_client):
        reg = self._register(webhook_client)
        resp = webhook_client.get(f"/webhooks/{reg['id']}")
        assert resp.status_code == 200
        assert resp.json()["id"] == reg["id"]

    def test_get_not_found(self, webhook_client):
        resp = webhook_client.get("/webhooks/nonexistent-id")
        assert resp.status_code == 404

    def test_update_url(self, webhook_client):
        reg = self._register(webhook_client)
        resp = webhook_client.patch(f"/webhooks/{reg['id']}", json={"url": "https://new.example.com/hook"})
        assert resp.status_code == 200
        assert resp.json()["url"] == "https://new.example.com/hook"

    def test_update_active_flag(self, webhook_client):
        reg = self._register(webhook_client)
        resp = webhook_client.patch(f"/webhooks/{reg['id']}", json={"active": False})
        assert resp.status_code == 200
        assert resp.json()["active"] is False

    def test_update_not_found(self, webhook_client):
        resp = webhook_client.patch("/webhooks/nonexistent", json={"active": False})
        assert resp.status_code == 404

    def test_delete(self, webhook_client):
        reg = self._register(webhook_client)
        resp = webhook_client.delete(f"/webhooks/{reg['id']}")
        assert resp.status_code == 204

    def test_delete_not_found(self, webhook_client):
        resp = webhook_client.delete("/webhooks/nonexistent")
        assert resp.status_code == 404

    def test_delete_then_get(self, webhook_client):
        reg = self._register(webhook_client)
        webhook_client.delete(f"/webhooks/{reg['id']}")
        resp = webhook_client.get(f"/webhooks/{reg['id']}")
        assert resp.status_code == 404


class TestManagerNotInit:
    def test_list_when_none(self, api_client):
        # api_client has _webhook_manager=None — should get 503
        resp = api_client.get("/webhooks")
        assert resp.status_code == 503

    def test_register_when_none(self, api_client):
        resp = api_client.post("/webhooks", json={
            "client_id": CLIENT_ID,
            "url": "https://x.com",
            "events": ["alert_analyzed"],
        })
        assert resp.status_code == 503
