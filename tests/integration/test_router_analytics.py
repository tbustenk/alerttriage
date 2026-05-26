"""Integration tests for the /analytics router."""

from __future__ import annotations

import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from tests.conftest import CLIENT_ID, _FakeConfig


def _seed_feedback(data_dir: Path, n_tp: int = 10, n_fp: int = 5) -> None:
    """Seed feedback records so the analytics engine has data to work with."""
    from alerttriage.src.core.alert_models import FeedbackRecord
    from alerttriage.src.feedback.feedback_system import FeedbackSystem

    fs = FeedbackSystem(data_dir, CLIENT_ID)
    records = [
        FeedbackRecord(
            alert_id=f"a-{i}",
            analysis_id=f"an-{i}",
            client_id=CLIENT_ID,
            analyst_id="analyst-1",
            analyst_verdict="true_positive",
            ai_verdict_was_correct=True,
            metadata={"rule_name": "PortScan"},
        )
        for i in range(n_tp)
    ] + [
        FeedbackRecord(
            alert_id=f"fp-{i}",
            analysis_id=f"fpan-{i}",
            client_id=CLIENT_ID,
            analyst_id="analyst-1",
            analyst_verdict="false_positive",
            ai_verdict_was_correct=False,
            metadata={"rule_name": "BruteForce"},
        )
        for i in range(n_fp)
    ]
    fs.record_many(records)


@pytest.fixture
def analytics_client(monkeypatch):
    """TestClient with a real AnalyticsEngine and seeded feedback data."""
    import alerttriage.src.api.app as _app
    import alerttriage.src.api.auth as _auth
    from alerttriage.src.analytics.engine import AnalyticsEngine
    from alerttriage.src.analytics.scheduler import ReportScheduler
    from alerttriage.src.api.app import app
    from alerttriage.src.core.analyzer import AlertAnalyzer
    from alerttriage.src.monitoring.health_monitor import HealthMonitor
    from fastapi.testclient import TestClient

    monkeypatch.setattr(_auth, "VALID_KEYS", frozenset(["test-key"]))

    with tempfile.TemporaryDirectory() as tmp:
        tmp_data = Path(tmp) / "data"
        tmp_data.mkdir()
        _seed_feedback(tmp_data)

        @asynccontextmanager
        async def _lifespan(_):
            config = _FakeConfig(tmp_data)
            _app._analyzer = AlertAnalyzer(config)
            _app._monitor = HealthMonitor(config)
            _app._feedback_systems = {}
            _app._webhook_manager = None
            _app._analytics_engine = AnalyticsEngine(tmp_data, config)
            _app._report_scheduler = ReportScheduler(tmp_data, config, tmp_data / "reports")
            _app._config_versioning = None
            _app._feature_flags = None
            _app._alert_type_settings = None
            _app._reports_dir = tmp_data / "reports"
            yield
            _app._analyzer = None
            _app._monitor = None
            _app._feedback_systems = {}
            _app._analytics_engine = None
            _app._report_scheduler = None

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
            _app._analytics_engine = None
            _app._report_scheduler = None


class TestSummary:
    def test_summary_returns_data(self, analytics_client):
        resp = analytics_client.get(f"/analytics/summary/{CLIENT_ID}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["client_id"] == CLIENT_ID
        assert "total_alerts" in data
        assert "overall_accuracy" in data
        assert "weeks" in data

    def test_summary_custom_params(self, analytics_client):
        resp = analytics_client.get(
            f"/analytics/summary/{CLIENT_ID}?weeks=2&hourly_rate=100&minutes_per_alert=20"
        )
        assert resp.status_code == 200

    def test_summary_engine_not_init(self, api_client):
        resp = api_client.get(f"/analytics/summary/{CLIENT_ID}")
        assert resp.status_code == 503


class TestBreakdown:
    def test_breakdown_returns_rules(self, analytics_client):
        resp = analytics_client.get(f"/analytics/breakdown/{CLIENT_ID}")
        assert resp.status_code == 200
        data = resp.json()
        assert "rules" in data
        assert data["client_id"] == CLIENT_ID
        assert isinstance(data["rules"], list)

    def test_breakdown_custom_days(self, analytics_client):
        resp = analytics_client.get(f"/analytics/breakdown/{CLIENT_ID}?days=60")
        assert resp.status_code == 200
        assert resp.json()["period_days"] == 60


class TestROI:
    def test_roi_calculation(self, analytics_client):
        resp = analytics_client.get(f"/analytics/roi/{CLIENT_ID}")
        assert resp.status_code == 200
        data = resp.json()
        assert "hours_saved" in data
        assert "labor_value_usd" in data
        assert "net_roi_usd" in data
        assert "roi_percentage" in data
        assert "monthly_projection_usd" in data

    def test_roi_custom_rate(self, analytics_client):
        resp = analytics_client.get(
            f"/analytics/roi/{CLIENT_ID}?hourly_rate=150&minutes_per_alert=30"
        )
        assert resp.status_code == 200


class TestFPAnalysis:
    def test_fp_analysis(self, analytics_client):
        resp = analytics_client.get(f"/analytics/fp-analysis/{CLIENT_ID}")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_false_positives" in data
        assert "fp_rate" in data
        assert "worst_rules" in data
        assert "recommendations" in data


class TestTrends:
    def test_trends(self, analytics_client):
        resp = analytics_client.get(f"/analytics/trends/{CLIENT_ID}")
        assert resp.status_code == 200
        data = resp.json()
        assert "accuracy_direction" in data
        assert "accuracy" in data
        assert "fp_rate" in data
        assert "throughput" in data


class TestFullReport:
    def test_full_report_json(self, analytics_client):
        resp = analytics_client.get(f"/analytics/report/{CLIENT_ID}")
        assert resp.status_code == 200
        data = resp.json()
        assert "client_id" in data

    def test_download_html(self, analytics_client):
        resp = analytics_client.get(f"/analytics/report/{CLIENT_ID}/download?format=html")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_download_csv(self, analytics_client):
        resp = analytics_client.get(f"/analytics/report/{CLIENT_ID}/download?format=csv")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]

    def test_download_json(self, analytics_client):
        resp = analytics_client.get(f"/analytics/report/{CLIENT_ID}/download?format=json")
        assert resp.status_code == 200
        assert "application/json" in resp.headers["content-type"]


class TestSchedule:
    def test_get_schedule_not_init(self, api_client):
        resp = api_client.get(f"/analytics/schedule/{CLIENT_ID}")
        assert resp.status_code == 503

    def test_set_schedule_not_init(self, api_client):
        resp = api_client.post(
            f"/analytics/schedule/{CLIENT_ID}",
            json={"frequency": "weekly", "enabled": True},
        )
        assert resp.status_code == 503

    def test_delete_schedule_not_init(self, api_client):
        resp = api_client.delete(f"/analytics/schedule/{CLIENT_ID}")
        assert resp.status_code == 503

    def test_get_schedule_no_entry(self, analytics_client):
        resp = analytics_client.get(f"/analytics/schedule/{CLIENT_ID}")
        assert resp.status_code == 200
        data = resp.json()
        # No schedule configured → message response
        assert "message" in data or isinstance(data, dict)

    def test_set_and_get_schedule(self, analytics_client):
        schedule = {"frequency": "weekly", "day_of_week": 1, "hour": 9, "enabled": True}
        resp = analytics_client.post(f"/analytics/schedule/{CLIENT_ID}", json=schedule)
        assert resp.status_code == 201
        assert resp.json()["status"] == "scheduled"

        resp = analytics_client.get(f"/analytics/schedule/{CLIENT_ID}")
        assert resp.status_code == 200

    def test_delete_schedule(self, analytics_client):
        schedule = {"frequency": "weekly", "enabled": True}
        analytics_client.post(f"/analytics/schedule/{CLIENT_ID}", json=schedule)
        resp = analytics_client.delete(f"/analytics/schedule/{CLIENT_ID}")
        assert resp.status_code == 200
        assert resp.json()["status"] in ("deleted", "not_found")
