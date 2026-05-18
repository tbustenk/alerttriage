"""Core alert analysis orchestrator."""

from __future__ import annotations

import time
from typing import Any

from alerttriage.src.core.alert_models import Alert
from alerttriage.src.core.result_models import AnalysisResult
from alerttriage.src.logger import get_logger
from alerttriage.src.anonymize import Anonymizer
from alerttriage.src.cost_controller import CostController
from alerttriage.src.models.model_router import ModelRouter
from alerttriage.config.config_manager import ConfigManager

log = get_logger(__name__)


class AlertAnalyzer:
    """
    Main entry point for alert triage.

    Orchestrates: anonymization → model routing → cost accounting → result.
    """

    def __init__(self, config: ConfigManager) -> None:
        self.config = config
        self.anonymizer = Anonymizer(config)
        self.cost_controller = CostController(config)
        self.router = ModelRouter(config)

    async def analyze(self, alert: Alert, *, dry_run: bool = False) -> AnalysisResult:
        """
        Analyse a single alert end-to-end.

        Args:
            alert: Normalised alert record.
            dry_run: Skip AI call; return a placeholder result for testing.

        Returns:
            AnalysisResult with verdict, reasoning, and cost metadata.

        Raises:
            CostLimitExceededError: If the client has exhausted its budget.
            ModelUnavailableError: If all configured backends fail.
        """
        log.info("analyzing_alert", alert_id=alert.id, client=alert.client_id,
                 rule=alert.rule_name, severity=alert.severity)

        self.cost_controller.check_limit(alert.client_id)

        anon_alert, reverse_map = self.anonymizer.anonymize(alert)

        if dry_run:
            return self._dry_run_result(alert)

        start = time.monotonic()
        result = await self.router.route(anon_alert)
        result.latency_ms = int((time.monotonic() - start) * 1000)

        result.alert_id = alert.id
        result.client_id = alert.client_id

        self.cost_controller.record(alert.client_id, result.cost_usd)

        log.info("analysis_complete", alert_id=alert.id, verdict=result.verdict,
                 confidence=result.confidence, cost_usd=result.cost_usd)
        return result

    async def analyze_batch(
        self,
        alerts: list[Alert],
        *,
        max_concurrency: int = 5,
    ) -> list[AnalysisResult]:
        """Analyse multiple alerts with bounded concurrency."""
        import asyncio

        sem = asyncio.Semaphore(max_concurrency)

        async def _bounded(a: Alert) -> AnalysisResult:
            async with sem:
                return await self.analyze(a)

        return await asyncio.gather(*[_bounded(a) for a in alerts])

    # ------------------------------------------------------------------
    def _dry_run_result(self, alert: Alert) -> AnalysisResult:
        from alerttriage.src.core.result_models import Verdict
        return AnalysisResult(
            alert_id=alert.id,
            client_id=alert.client_id,
            model_id="dry-run",
            verdict=Verdict.UNKNOWN,
            confidence=0.0,
            summary="[dry-run] No AI call made.",
            reasoning="",
        )
