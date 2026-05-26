"""Shared pytest fixtures for the AlertTriage test suite.

These fixtures are available to all tests.  Existing tests continue to use
their own inline ``setup_method`` / ``teardown_method`` approach and are
unaffected by this module.
"""

from __future__ import annotations

import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml

CLIENT_ID = "test-client"


# ---------------------------------------------------------------------------
# Minimal config shim
# ---------------------------------------------------------------------------


class _FakeConfig:
    """Minimal ConfigManager shim that satisfies AlertAnalyzer / HealthMonitor."""

    def __init__(self, data_dir: str | Path) -> None:
        from alerttriage.config.config_manager import (
            ConcurrencyConfig,
            FeedbackConfig,
            LearningConfig,
            RetryConfig,
        )

        self.log_level = "WARNING"
        self.json_logs = False
        self.anonymize_salt = "test-salt-32-chars-xxxxxxxxxxx"
        self.data_dir = Path(data_dir)
        self.reports_dir = "reports"
        self.default_model_id = "claude-sonnet"
        self.models_config = {"default": "claude-sonnet", "models": {}}
        self.concurrency = ConcurrencyConfig()
        self.retry = RetryConfig()
        self.learning = LearningConfig()
        self.feedback = FeedbackConfig()

    def get_client(self, client_id: str) -> dict[str, Any]:
        return {"client_id": client_id, "model": "claude-sonnet", "features": {}}

    def get_learning(self, client_id: str):  # type: ignore[return]
        return self.learning

    def list_clients(self) -> list[str]:
        return [CLIENT_ID]


# ---------------------------------------------------------------------------
# Directory fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def tmp_data_dir(tmp_dir):
    d = tmp_dir / "data"
    d.mkdir()
    return d


@pytest.fixture
def tmp_config_dir(tmp_dir):
    d = tmp_dir / "config"
    d.mkdir()
    (d / "client_configs").mkdir()
    return d


@pytest.fixture
def tmp_config_with_client(tmp_config_dir):
    """Config dir that contains a minimal client YAML."""
    client_cfg = {
        "client_id": CLIENT_ID,
        "model": "claude-sonnet",
        "features": {"analytics": True, "webhooks": True},
        "alert_types": {},
    }
    (tmp_config_dir / "client_configs" / f"{CLIENT_ID}.yaml").write_text(
        yaml.safe_dump(client_cfg), encoding="utf-8"
    )
    return tmp_config_dir


# ---------------------------------------------------------------------------
# Core object fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_config(tmp_data_dir):
    return _FakeConfig(tmp_data_dir)


@pytest.fixture
def feedback_system(tmp_data_dir):
    from alerttriage.src.feedback.feedback_system import FeedbackSystem

    return FeedbackSystem(tmp_data_dir, CLIENT_ID)


@pytest.fixture
def populated_feedback(feedback_system):
    """FeedbackSystem with 20 records — 15 TP (PortScan) + 5 FP (BruteForce)."""
    from alerttriage.src.core.alert_models import FeedbackRecord

    tps = [
        FeedbackRecord(
            alert_id=f"alert-{i}",
            analysis_id=f"analysis-{i}",
            client_id=CLIENT_ID,
            analyst_id="analyst-1",
            analyst_verdict="true_positive",
            ai_verdict_was_correct=True,
            metadata={"rule_name": "PortScan"},
        )
        for i in range(15)
    ]
    fps = [
        FeedbackRecord(
            alert_id=f"fp-alert-{i}",
            analysis_id=f"fp-analysis-{i}",
            client_id=CLIENT_ID,
            analyst_id="analyst-1",
            analyst_verdict="false_positive",
            ai_verdict_was_correct=False,
            metadata={"rule_name": "BruteForce"},
        )
        for i in range(5)
    ]
    feedback_system.record_many(tps + fps)
    return feedback_system


@pytest.fixture
def sample_alert():
    from alerttriage.src.core.alert_models import (
        Alert,
        AlertContext,
        AlertSeverity,
        AlertSource,
    )

    return Alert(
        client_id=CLIENT_ID,
        source=AlertSource.MANUAL,
        rule_name="BruteForce",
        severity=AlertSeverity.HIGH,
        title="Brute force login detected",
        description="50 failed login attempts from 192.168.1.100 in 5 minutes.",
        context=AlertContext(
            host_info={"hostname": "web-server-01", "ip": "10.0.0.5"},
            user_info={"username": "admin"},
            network_info={"src_ip": "192.168.1.100"},
        ),
        tags=["login", "brute-force"],
    )


# ---------------------------------------------------------------------------
# FastAPI test client
# ---------------------------------------------------------------------------


@pytest.fixture
def api_client(tmp_dir, monkeypatch):
    """FastAPI TestClient with isolated tmp data, noop lifespan, test API key."""
    import alerttriage.src.api.app as _app
    import alerttriage.src.api.auth as _auth
    from alerttriage.src.api.app import app
    from alerttriage.src.core.analyzer import AlertAnalyzer
    from alerttriage.src.monitoring.health_monitor import HealthMonitor
    from fastapi.testclient import TestClient

    tmp_data = tmp_dir / "data"
    tmp_data.mkdir()

    monkeypatch.setattr(_auth, "VALID_KEYS", frozenset(["test-key"]))

    @asynccontextmanager
    async def _test_lifespan(_):  # type: ignore[misc]
        config = _FakeConfig(tmp_data)
        _app._analyzer = AlertAnalyzer(config)
        _app._monitor = HealthMonitor(config)
        _app._feedback_systems = {}
        _app._webhook_manager = None
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

    original = app.router.lifespan_context
    app.router.lifespan_context = _test_lifespan
    try:
        with TestClient(app, raise_server_exceptions=True) as client:
            client.headers.update({"X-API-Key": "test-key"})
            yield client
    finally:
        app.router.lifespan_context = original
        # Reset singletons
        _app._analyzer = None
        _app._monitor = None
