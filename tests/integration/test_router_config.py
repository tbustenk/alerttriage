"""Integration tests for the /config router."""

from __future__ import annotations

import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import yaml

from tests.conftest import CLIENT_ID, _FakeConfig


@pytest.fixture
def config_client(monkeypatch):
    """TestClient with real ConfigVersioning, FeatureFlagManager, and AlertTypeSettings."""
    import alerttriage.src.api.app as _app
    import alerttriage.src.api.auth as _auth
    from alerttriage.src.api.app import app
    from alerttriage.src.config_ext.alert_type_settings import AlertTypeSettingsManager
    from alerttriage.src.config_ext.feature_flags import FeatureFlagManager
    from alerttriage.src.config_ext.versioning import ConfigVersioning
    from alerttriage.src.core.analyzer import AlertAnalyzer
    from alerttriage.src.monitoring.health_monitor import HealthMonitor
    from fastapi.testclient import TestClient

    monkeypatch.setattr(_auth, "VALID_KEYS", frozenset(["test-key"]))

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        tmp_data = tmp_dir / "data"
        tmp_data.mkdir()
        tmp_cfg = tmp_dir / "config"
        tmp_cfg.mkdir()
        (tmp_cfg / "client_configs").mkdir()

        # Write a minimal client config
        client_cfg = {"client_id": CLIENT_ID, "model": "claude-sonnet"}
        (tmp_cfg / "client_configs" / f"{CLIENT_ID}.yaml").write_text(
            yaml.safe_dump(client_cfg), encoding="utf-8"
        )

        @asynccontextmanager
        async def _lifespan(_):
            config = _FakeConfig(tmp_data)
            _app._analyzer = AlertAnalyzer(config)
            _app._monitor = HealthMonitor(config)
            _app._feedback_systems = {}
            _app._webhook_manager = None
            _app._analytics_engine = None
            _app._report_scheduler = None
            _app._config_versioning = ConfigVersioning(tmp_data, tmp_cfg)
            _app._feature_flags = FeatureFlagManager(tmp_cfg, config)
            _app._alert_type_settings = AlertTypeSettingsManager(tmp_cfg, config)
            _app._reports_dir = tmp_data / "reports"
            _app._CONFIG_DIR = tmp_cfg
            yield
            _app._analyzer = None
            _app._monitor = None
            _app._feedback_systems = {}
            _app._config_versioning = None
            _app._feature_flags = None
            _app._alert_type_settings = None

        original = app.router.lifespan_context
        app.router.lifespan_context = _lifespan
        try:
            with TestClient(app, raise_server_exceptions=True) as client:
                client.headers.update({"X-API-Key": "test-key"})
                yield client, tmp_cfg
        finally:
            app.router.lifespan_context = original
            _app._analyzer = None
            _app._monitor = None
            _app._feedback_systems = {}
            _app._config_versioning = None
            _app._feature_flags = None
            _app._alert_type_settings = None


class TestClientConfig:
    def test_get_client_config(self, config_client):
        client, _ = config_client
        resp = client.get(f"/config/{CLIENT_ID}")
        assert resp.status_code == 200

    def test_list_clients(self, config_client):
        client, _ = config_client
        resp = client.get("/config/clients")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


class TestVersioning:
    def test_list_versions_empty(self, config_client):
        client, _ = config_client
        resp = client.get(f"/config/{CLIENT_ID}/versions")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_create_snapshot(self, config_client):
        client, _ = config_client
        resp = client.post(f"/config/{CLIENT_ID}/snapshot?comment=test-snap")
        assert resp.status_code == 201
        data = resp.json()
        assert "version_id" in data
        assert data["status"] == "created"

    def test_list_versions_after_snapshot(self, config_client):
        client, _ = config_client
        client.post(f"/config/{CLIENT_ID}/snapshot?comment=v1")
        resp = client.get(f"/config/{CLIENT_ID}/versions")
        assert resp.status_code == 200
        versions = resp.json()
        assert len(versions) == 1
        assert versions[0]["comment"] == "v1"

    def test_get_snapshot(self, config_client):
        client, _ = config_client
        snap = client.post(f"/config/{CLIENT_ID}/snapshot?comment=getme").json()
        resp = client.get(f"/config/{CLIENT_ID}/versions/{snap['version_id']}")
        assert resp.status_code == 200

    def test_get_snapshot_not_found(self, config_client):
        client, _ = config_client
        resp = client.get(f"/config/{CLIENT_ID}/versions/nonexistent-id")
        assert resp.status_code == 404

    def test_rollback(self, config_client):
        client, _ = config_client
        snap = client.post(f"/config/{CLIENT_ID}/snapshot?comment=rollback-target").json()
        resp = client.post(f"/config/{CLIENT_ID}/rollback/{snap['version_id']}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "rolled_back"
        assert data["version_id"] == snap["version_id"]

    def test_rollback_not_found(self, config_client):
        client, _ = config_client
        resp = client.post(f"/config/{CLIENT_ID}/rollback/no-such-version")
        assert resp.status_code == 404

    def test_versioning_not_init(self, api_client):
        resp = api_client.get(f"/config/{CLIENT_ID}/versions")
        assert resp.status_code == 503


class TestFeatureFlags:
    def test_list_global_flags(self, config_client):
        client, _ = config_client
        resp = client.get("/config/features/global")
        assert resp.status_code == 200
        flags = resp.json()
        assert "analytics" in flags

    def test_list_client_flags(self, config_client):
        client, _ = config_client
        resp = client.get(f"/config/features/{CLIENT_ID}")
        assert resp.status_code == 200
        assert isinstance(resp.json(), dict)

    def test_update_client_flags(self, config_client):
        client, tmp_cfg = config_client
        resp = client.put(
            f"/config/features/{CLIENT_ID}",
            json={"analytics": {"enabled": False}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "updated"

    def test_update_flags_missing_client(self, config_client):
        client, _ = config_client
        resp = client.put(
            "/config/features/no-such-client",
            json={"analytics": {"enabled": False}},
        )
        assert resp.status_code == 404

    def test_flags_not_init(self, api_client):
        resp = api_client.get("/config/features/global")
        assert resp.status_code == 503


class TestAlertTypes:
    def test_list_alert_types(self, config_client):
        client, _ = config_client
        resp = client.get("/config/alert-types/all")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_get_alert_type_defaults(self, config_client):
        client, _ = config_client
        resp = client.get("/config/alert-types/BruteForce")
        assert resp.status_code == 200
        data = resp.json()
        assert "confidence_threshold" in data

    def test_update_alert_type_global(self, config_client):
        client, _ = config_client
        resp = client.put(
            "/config/alert-types/NewRule",
            json={"priority": "high", "auto_close": False},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "updated"
        assert data["rule"] == "NewRule"

    def test_alert_types_not_init(self, api_client):
        resp = api_client.get("/config/alert-types/all")
        assert resp.status_code == 503


class TestReload:
    def test_reload_with_singletons(self, config_client):
        client, _ = config_client
        resp = client.post("/config/reload")
        assert resp.status_code == 200
        assert resp.json()["status"] == "reloaded"

    def test_reload_without_singletons(self, api_client):
        # flags and alert-types are None — reload still returns 200
        resp = api_client.post("/config/reload")
        assert resp.status_code == 200
