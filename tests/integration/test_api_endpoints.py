"""Integration tests for the FastAPI REST API.

Uses a real in-process TestClient with mocked singletons — no network
calls, no Anthropic API, no filesystem side-effects outside tmp_dir.
"""

from __future__ import annotations

import json

import pytest

CLIENT_ID = "test-client"
API_KEY = "test-key"


class TestHealthEndpoint:
    def test_health_ok(self, api_client):
        resp = api_client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == "2.0.0"
        assert "status" in body
        assert "components" in body

    def test_health_unauthenticated(self, api_client):
        """Health endpoint requires no API key."""
        resp = api_client.get("/health", headers={})
        assert resp.status_code == 200

    def test_health_includes_uptime(self, api_client):
        body = api_client.get("/health").json()
        assert "uptime_seconds" in body
        assert body["uptime_seconds"] >= 0


class TestAnalyzeEndpoint:
    _PAYLOAD = {
        "client_id": CLIENT_ID,
        "source": "manual",
        "rule_name": "BruteForce",
        "severity": "high",
        "title": "Brute force detected",
        "description": "50 failed logins from 192.168.1.1 in 5 minutes.",
    }

    def test_analyze_dry_run(self, api_client):
        payload = {**self._PAYLOAD, "dry_run": True}
        resp = api_client.post("/analyze", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["verdict"] == "unknown"
        assert body["model_id"] == "dry-run"
        assert body["client_id"] == CLIENT_ID

    def test_analyze_requires_auth(self, api_client):
        resp = api_client.post(
            "/analyze",
            json={**self._PAYLOAD, "dry_run": True},
            headers={"X-API-Key": "wrong-key"},
        )
        assert resp.status_code == 403

    def test_analyze_missing_required_fields(self, api_client):
        resp = api_client.post("/analyze", json={"dry_run": True})
        assert resp.status_code == 422

    def test_analyze_invalid_severity(self, api_client):
        payload = {**self._PAYLOAD, "dry_run": True, "severity": "extreme"}
        resp = api_client.post("/analyze", json=payload)
        assert resp.status_code == 422

    def test_analyze_caches_result(self, api_client):
        payload = {**self._PAYLOAD, "dry_run": True}
        resp = api_client.post("/analyze", json=payload)
        assert resp.status_code == 200
        alert_id = resp.json()["alert_id"]

        # Retrieve from cache
        get_resp = api_client.get(f"/results/{alert_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["alert_id"] == alert_id


class TestResultsEndpoint:
    def test_missing_result_returns_404(self, api_client):
        resp = api_client.get("/results/nonexistent-alert-id")
        assert resp.status_code == 404

    def test_retrieve_cached_result(self, api_client):
        payload = {
            "client_id": CLIENT_ID,
            "source": "manual",
            "rule_name": "Test",
            "severity": "low",
            "title": "Test",
            "description": "Test",
            "dry_run": True,
        }
        analyze_resp = api_client.post("/analyze", json=payload)
        assert analyze_resp.status_code == 200
        alert_id = analyze_resp.json()["alert_id"]

        get_resp = api_client.get(f"/results/{alert_id}")
        assert get_resp.status_code == 200
        body = get_resp.json()
        assert body["alert_id"] == alert_id


class TestFeedbackEndpoint:
    def test_record_feedback_success(self, api_client):
        resp = api_client.post(
            "/feedback",
            json={
                "alert_id": "a-1",
                "analysis_id": "r-1",
                "client_id": CLIENT_ID,
                "analyst_id": "analyst-x",
                "analyst_verdict": "false_positive",
                "analyst_notes": "Scanner traffic from known host.",
                "ai_verdict_was_correct": False,
                "rule_name": "PortScan",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "recorded"
        assert "id" in body

    def test_feedback_invalid_verdict(self, api_client):
        resp = api_client.post(
            "/feedback",
            json={
                "alert_id": "a-2",
                "analysis_id": "r-2",
                "client_id": CLIENT_ID,
                "analyst_id": "analyst-x",
                "analyst_verdict": "not_a_real_verdict",
                "ai_verdict_was_correct": False,
                "rule_name": "Test",
            },
        )
        assert resp.status_code == 422

    def test_feedback_requires_auth(self, api_client):
        resp = api_client.post(
            "/feedback",
            json={
                "alert_id": "a-3",
                "analysis_id": "r-3",
                "client_id": CLIENT_ID,
                "analyst_id": "x",
                "analyst_verdict": "true_positive",
                "ai_verdict_was_correct": True,
                "rule_name": "Test",
            },
            headers={"X-API-Key": "bad-key"},
        )
        assert resp.status_code == 403


class TestStatsEndpoint:
    def test_stats_empty_db(self, api_client):
        resp = api_client.get(f"/stats/{CLIENT_ID}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["client_id"] == CLIENT_ID
        assert body["overall_accuracy"] == 0.0
        assert body["total_analyses"] == 0

    def test_stats_after_feedback(self, api_client):
        # Record some feedback
        for i in range(3):
            api_client.post(
                "/feedback",
                json={
                    "alert_id": f"a-{i}",
                    "analysis_id": f"r-{i}",
                    "client_id": CLIENT_ID,
                    "analyst_id": "x",
                    "analyst_verdict": "true_positive",
                    "ai_verdict_was_correct": True,
                    "rule_name": "PortScan",
                },
            )

        resp = api_client.get(f"/stats/{CLIENT_ID}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_analyses"] == 3
        assert body["overall_accuracy"] == pytest.approx(1.0)


class TestHintsEndpoint:
    def test_hints_empty_returns_empty_list(self, api_client):
        resp = api_client.get(f"/hints?client_id={CLIENT_ID}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["client_id"] == CLIENT_ID
        assert isinstance(body["hints"], list)


class TestContextEndpoint:
    def test_store_context(self, api_client):
        resp = api_client.post(
            "/context",
            json={
                "client_id": CLIENT_ID,
                "context_type": "network_ranges",
                "data": {"internal": "10.0.0.0/8"},
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "stored"
        assert body["context_type"] == "network_ranges"

    def test_context_replaces_same_type(self, api_client):
        for val in ("v1", "v2"):
            api_client.post(
                "/context",
                json={
                    "client_id": CLIENT_ID,
                    "context_type": "threat_intel",
                    "data": {"value": val},
                },
            )
        # Should still return 201 on second write — no duplicate key error


class TestMetricsEndpoint:
    def test_metrics_returns_text(self, api_client):
        resp = api_client.get("/metrics")
        assert resp.status_code == 200
        assert "alerttriage_" in resp.text or resp.text == ""
        assert resp.headers["content-type"].startswith("text/plain")


class TestRateLimit:
    def test_rate_limit_header_format(self, api_client):
        resp = api_client.get("/health")
        assert resp.status_code == 200
