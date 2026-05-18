"""Integration-style tests for AlertAnalyzer (dry-run mode — no AI calls)."""

import asyncio
import pytest

from alerttriage.src.core.alert_models import Alert, AlertSeverity, AlertSource
from alerttriage.src.core.result_models import Verdict


class FakeConfig:
    log_level = "INFO"
    json_logs = False
    anonymize_salt = "test-salt"
    data_dir = "data"
    reports_dir = "reports"
    default_model_id = "claude-sonnet"

    def get_client(self, client_id):
        return {"client_id": client_id, "model": "claude-sonnet"}


def _make_alert(client_id="test"):
    return Alert(
        client_id=client_id,
        source=AlertSource.MANUAL,
        rule_name="Brute Force",
        severity=AlertSeverity.HIGH,
        title="Brute force detected",
        description="50 failed logins in 5 minutes.",
    )


class TestAlertAnalyzerDryRun:
    def setup_method(self):
        from alerttriage.src.core.analyzer import AlertAnalyzer
        self.analyzer = AlertAnalyzer(FakeConfig())

    def test_dry_run_returns_unknown(self):
        alert = _make_alert()
        result = asyncio.run(self.analyzer.analyze(alert, dry_run=True))
        assert result.verdict == Verdict.UNKNOWN
        assert result.model_id == "dry-run"
        assert result.alert_id == alert.id

    def test_batch_dry_run(self):
        alerts = [_make_alert(client_id="test") for _ in range(5)]
        results = asyncio.run(self.analyzer.analyze_batch(alerts))
        assert len(results) == 5
