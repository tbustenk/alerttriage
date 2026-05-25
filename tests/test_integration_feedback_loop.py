"""Integration test for the alert → feedback → learning → improved-analysis loop.

We don't call a real model. We register a fake backend that records every
system prompt it sees, then verify:

1. The first analysis uses the base prompt (no feedback yet).
2. After recording many FP feedback rows for a rule, ``invalidate_prompt_cache``
   refreshes hints and the next analysis sees an enhanced prompt that mentions
   the offending rule.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from alerttriage.config.config_manager import (
    ConcurrencyConfig,
    FeedbackConfig,
    LearningConfig,
    RetryConfig,
)
from alerttriage.src.core.alert_models import Alert, AlertSeverity, AlertSource, FeedbackRecord
from alerttriage.src.core.analyzer import AlertAnalyzer
from alerttriage.src.core.result_models import AnalysisResult, Verdict
from alerttriage.src.feedback.feedback_system import FeedbackSystem


class _RecordingBackend:
    """Captures the system prompt for every call; returns a canned verdict."""

    def __init__(self) -> None:
        self.model_id = "fake-recorder"
        self.prompts: list[str] = []

    async def analyze(
        self,
        alert: Alert,
        *,
        system_prompt: str,
        retry_config: Any | None = None,
    ) -> AnalysisResult:
        self.prompts.append(system_prompt)
        return AnalysisResult(
            alert_id=alert.id,
            client_id=alert.client_id,
            model_id=self.model_id,
            verdict=Verdict.UNKNOWN,
            confidence=0.5,
            summary="recorded",
            reasoning="",
            cost_usd=0.0,
        )


class _Cfg:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self.reports_dir = "reports"
        self.log_level = "INFO"
        self.json_logs = False
        self.anonymize_salt = "test-salt"
        self.default_model_id = "fake-recorder"
        self.models_config = {"default": "fake-recorder", "models": {}}
        self.concurrency = ConcurrencyConfig()
        self.retry = RetryConfig(max_attempts=1)
        self.learning = LearningConfig(fp_rate_threshold=0.5, min_sample_size=5, cache_ttl_sec=60)
        self.feedback = FeedbackConfig()

    def get_client(self, _id: str) -> dict:
        return {"client_id": _id, "model": "fake-recorder"}

    def get_learning(self, _id: str) -> LearningConfig:
        return self.learning


def _alert() -> Alert:
    return Alert(
        client_id="acme",
        source=AlertSource.MANUAL,
        rule_name="Vuln Scanner",
        severity=AlertSeverity.MEDIUM,
        title="suspicious scan",
        description="lots of port scans from a single host",
    )


def _fb(rule: str = "Vuln Scanner") -> FeedbackRecord:
    return FeedbackRecord(
        alert_id="alert",
        analysis_id="analysis",
        client_id="acme",
        analyst_id="alice",
        analyst_verdict="false_positive",
        ai_verdict_was_correct=False,
        metadata={"rule_name": rule},
    )


def test_feedback_loop_changes_subsequent_prompts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _Cfg(tmp)
        analyzer = AlertAnalyzer(cfg)
        backend = _RecordingBackend()
        # Swap the real backend dict for our recorder. ModelRouter looks up by id.
        analyzer.router._backends["fake-recorder"] = backend  # type: ignore[attr-defined]

        # First call: no feedback yet, prompt is the base.
        asyncio.run(analyzer.analyze(_alert()))
        assert "Vuln Scanner" not in backend.prompts[0]
        assert "Client-specific guidance" not in backend.prompts[0]

        # Record enough FP feedback to cross the learning thresholds.
        store = FeedbackSystem(Path(tmp), "acme")
        store.record_many([_fb() for _ in range(6)])

        # Invalidate the prompt cache so hints refresh on the next call.
        analyzer.invalidate_prompt_cache("acme")
        asyncio.run(analyzer.analyze(_alert()))
        enhanced = backend.prompts[1]
        assert "Vuln Scanner" in enhanced
        assert "Client-specific guidance" in enhanced


def test_batch_continues_when_single_alert_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _Cfg(tmp)
        analyzer = AlertAnalyzer(cfg)

        class _FailingBackend:
            model_id = "fake-recorder"

            def __init__(self) -> None:
                self.calls = 0

            async def analyze(self, alert, *, system_prompt, retry_config=None):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("backend exploded")
                return AnalysisResult(
                    alert_id=alert.id,
                    client_id=alert.client_id,
                    model_id="fake-recorder",
                    verdict=Verdict.BENIGN,
                    confidence=0.7,
                    summary="ok",
                    reasoning="",
                )

        analyzer.router._backends["fake-recorder"] = _FailingBackend()  # type: ignore[attr-defined]
        alerts = [_alert() for _ in range(3)]
        results = asyncio.run(analyzer.analyze_batch(alerts, max_concurrency=1))
        assert len(results) == 3
        # One of them should be the error placeholder.
        errored = [r for r in results if r.model_id == "error"]
        assert len(errored) == 1
