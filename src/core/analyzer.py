"""Core alert analysis orchestrator.

Wires together:

* :class:`alerttriage.src.cost_controller.CostController` — budget guard.
* :class:`alerttriage.src.anonymize.Anonymizer` — PII pseudonymisation.
* :class:`alerttriage.src.feedback.prompt_enhancer.PromptEnhancer` —
  prepends per-client learning hints to the system prompt.
* :class:`alerttriage.src.models.model_router.ModelRouter` — picks a backend
  and runs the call with the shared retry policy.

The orchestrator is the **only** place that knows about the full pipeline.
Backends and connectors stay narrow and replaceable.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from alerttriage.src.anonymize import Anonymizer
from alerttriage.src.core.alert_models import Alert
from alerttriage.src.core.result_models import AnalysisResult, Verdict
from alerttriage.src.cost_controller import CostController
from alerttriage.src.feedback.feedback_system import FeedbackSystem
from alerttriage.src.feedback.learning_engine import LearningEngine
from alerttriage.src.feedback.prompt_enhancer import PromptEnhancer
from alerttriage.src.logger import get_logger
from alerttriage.src.models.claude_backend import BASE_SYSTEM_PROMPT
from alerttriage.src.models.model_router import ModelRouter

if TYPE_CHECKING:
    from alerttriage.config.config_manager import ConfigManager

log = get_logger(__name__)


class AlertAnalyzer:
    """Main entry point for alert triage.

    Orchestrates: cost-check → anonymise → enhance prompt → route → record cost.
    """

    def __init__(
        self,
        config: ConfigManager,
        *,
        prompt_enhancer: PromptEnhancer | None = None,
    ) -> None:
        """Args:
        config: Validated :class:`ConfigManager`.
        prompt_enhancer: Optional override (mostly for tests). When ``None``,
            an enhancer wired to per-client :class:`FeedbackSystem` instances
            is built from config.
        """
        self.config = config
        self.anonymizer = Anonymizer(config)
        self.cost_controller = CostController(config)
        self.router = ModelRouter(config)
        self.prompt_enhancer = prompt_enhancer or self._build_prompt_enhancer()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def analyze(self, alert: Alert, *, dry_run: bool = False) -> AnalysisResult:
        """Analyse a single alert end-to-end.

        Args:
            alert: Normalised alert record.
            dry_run: Skip the AI call; return a placeholder result for tests
                and pre-flight connectivity checks.

        Returns:
            AnalysisResult with verdict, reasoning, and cost metadata.

        Raises:
            CostLimitExceededError: Client has exhausted its budget.
            ModelUnavailableError: All configured backends failed.
        """
        log.info(
            "analyzing_alert",
            alert_id=alert.id,
            client=alert.client_id,
            rule=alert.rule_name,
            severity=alert.severity.value,
        )

        self.cost_controller.check_limit(alert.client_id)
        anon_alert, _reverse_map = self.anonymizer.anonymize(alert)

        if dry_run:
            return self._dry_run_result(alert)

        system_prompt = self.prompt_enhancer.enhance(BASE_SYSTEM_PROMPT, client_id=alert.client_id)

        start = time.monotonic()
        result = await self.router.route(
            anon_alert,
            system_prompt=system_prompt,
            retry_config=self.config.retry,
        )
        result.latency_ms = int((time.monotonic() - start) * 1000)
        result.alert_id = alert.id
        result.client_id = alert.client_id

        self.cost_controller.record(alert.client_id, result.cost_usd)

        log.info(
            "analysis_complete",
            alert_id=alert.id,
            verdict=result.verdict.value,
            confidence=result.confidence,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
        )
        return result

    async def analyze_batch(
        self,
        alerts: list[Alert],
        *,
        max_concurrency: int | None = None,
    ) -> list[AnalysisResult]:
        """Analyse many alerts with bounded concurrency.

        Failed analyses are logged and replaced with an ``UNKNOWN`` placeholder
        so callers always get a result per input alert.
        """
        if not alerts:
            return []

        concurrency = max_concurrency or self.config.concurrency.max_in_flight
        sem = asyncio.Semaphore(max(1, concurrency))

        async def _bounded(alert: Alert) -> AnalysisResult:
            async with sem:
                try:
                    return await self.analyze(alert)
                except Exception as exc:  # noqa: BLE001 — never poison the gather
                    log.error("batch_alert_failed", alert_id=alert.id, error=str(exc))
                    return _error_result(alert, str(exc))

        return await asyncio.gather(*[_bounded(a) for a in alerts])

    def invalidate_prompt_cache(self, client_id: str | None = None) -> None:
        """Force the prompt enhancer to rebuild hints on the next call."""
        self.prompt_enhancer.invalidate(client_id)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_prompt_enhancer(self) -> PromptEnhancer:
        from pathlib import Path

        data_dir = Path(self.config.data_dir)
        learning_cfg = self.config.learning
        feedback_cache: dict[str, FeedbackSystem] = {}

        def factory(client_id: str) -> LearningEngine:
            store = feedback_cache.get(client_id)
            if store is None:
                store = FeedbackSystem(data_dir, client_id)
                feedback_cache[client_id] = store
            per_client = self.config.get_learning(client_id)
            return LearningEngine(
                store,
                fp_rate_threshold=per_client.fp_rate_threshold,
                min_sample_size=per_client.min_sample_size,
            )

        return PromptEnhancer(
            factory,
            cache_ttl_sec=learning_cfg.cache_ttl_sec,
            max_hints=learning_cfg.max_hints,
        )

    def _dry_run_result(self, alert: Alert) -> AnalysisResult:
        return AnalysisResult(
            alert_id=alert.id,
            client_id=alert.client_id,
            model_id="dry-run",
            verdict=Verdict.UNKNOWN,
            confidence=0.0,
            summary="[dry-run] No AI call made.",
            reasoning="",
        )


def _error_result(alert: Alert, error: str) -> AnalysisResult:
    """Placeholder verdict so a single failure doesn't drop the whole batch."""
    return AnalysisResult(
        alert_id=alert.id,
        client_id=alert.client_id,
        model_id="error",
        verdict=Verdict.UNKNOWN,
        confidence=0.0,
        summary=f"Analysis failed: {error[:200]}",
        reasoning="",
        metadata={"error": error[:1000]},
    )


__all__ = ["AlertAnalyzer"]
