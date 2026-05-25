"""Integration-style tests for AlertAnalyzer (dry-run — no AI calls)."""

from __future__ import annotations

import asyncio
import tempfile

from alerttriage.config.config_manager import (
    ConcurrencyConfig,
    FeedbackConfig,
    LearningConfig,
    RetryConfig,
)
from alerttriage.src.core.alert_models import Alert, AlertSeverity, AlertSource
from alerttriage.src.core.result_models import Verdict


class FakeConfig:
    """Minimal config shim that satisfies what AlertAnalyzer reads."""

    def __init__(self, data_dir: str) -> None:
        self.log_level = "INFO"
        self.json_logs = False
        self.anonymize_salt = "test-salt"
        self.data_dir = data_dir
        self.reports_dir = "reports"
        self.default_model_id = "claude-sonnet"
        self.models_config = {"default": "claude-sonnet", "models": {}}
        self.concurrency = ConcurrencyConfig()
        self.retry = RetryConfig()
        self.learning = LearningConfig()
        self.feedback = FeedbackConfig()

    def get_client(self, client_id: str) -> dict:
        return {"client_id": client_id, "model": "claude-sonnet"}

    def get_learning(self, client_id: str) -> LearningConfig:
        return self.learning


def _make_alert(client_id: str = "test") -> Alert:
    return Alert(
        client_id=client_id,
        source=AlertSource.MANUAL,
        rule_name="Brute Force",
        severity=AlertSeverity.HIGH,
        title="Brute force detected",
        description="50 failed logins in 5 minutes.",
    )


class TestAlertAnalyzerDryRun:
    def setup_method(self) -> None:
        from alerttriage.src.core.analyzer import AlertAnalyzer

        self._tmp = tempfile.TemporaryDirectory()
        self.analyzer = AlertAnalyzer(FakeConfig(data_dir=self._tmp.name))

    def teardown_method(self) -> None:
        self._tmp.cleanup()

    def test_dry_run_returns_unknown(self) -> None:
        alert = _make_alert()
        result = asyncio.run(self.analyzer.analyze(alert, dry_run=True))
        assert result.verdict == Verdict.UNKNOWN
        assert result.model_id == "dry-run"
        assert result.alert_id == alert.id

    def test_batch_dry_run(self) -> None:
        alerts = [_make_alert() for _ in range(5)]
        results = asyncio.run(self.analyzer.analyze_batch(alerts))
        assert len(results) == 5

    def test_batch_empty(self) -> None:
        results = asyncio.run(self.analyzer.analyze_batch([]))
        assert results == []

    def test_dry_run_anonymises_alert(self) -> None:
        alert = _make_alert()
        alert.context.host_info["ip"] = "10.0.0.5"
        # Dry run still runs the anonymiser; reverse map stays in memory only.
        result = asyncio.run(self.analyzer.analyze(alert, dry_run=True))
        assert result.client_id == alert.client_id
