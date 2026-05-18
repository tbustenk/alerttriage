"""Performance metric calculations for client dashboards and reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Sequence

from alerttriage.src.core.result_models import AnalysisResult, Verdict
from alerttriage.src.core.alert_models import FeedbackRecord


@dataclass
class VerdictBreakdown:
    true_positive: int = 0
    false_positive: int = 0
    needs_escalation: int = 0
    needs_investigation: int = 0
    benign: int = 0
    unknown: int = 0

    @property
    def total(self) -> int:
        return (
            self.true_positive + self.false_positive + self.needs_escalation
            + self.needs_investigation + self.benign + self.unknown
        )


@dataclass
class CostSummary:
    total_usd: float = 0.0
    total_tokens: int = 0
    avg_cost_per_alert: float = 0.0
    by_model: dict[str, float] = field(default_factory=dict)


@dataclass
class AccuracyMetrics:
    overall_accuracy: float = 0.0
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    sample_size: int = 0


@dataclass
class ClientMetrics:
    client_id: str
    window_start: datetime
    window_end: datetime
    alert_count: int
    verdict_breakdown: VerdictBreakdown
    cost_summary: CostSummary
    accuracy: AccuracyMetrics
    avg_latency_ms: float
    avg_confidence: float


def compute_verdict_breakdown(results: Sequence[AnalysisResult]) -> VerdictBreakdown:
    bd = VerdictBreakdown()
    for r in results:
        match r.verdict:
            case Verdict.TRUE_POSITIVE:      bd.true_positive += 1
            case Verdict.FALSE_POSITIVE:     bd.false_positive += 1
            case Verdict.NEEDS_ESCALATION:   bd.needs_escalation += 1
            case Verdict.NEEDS_INVESTIGATION: bd.needs_investigation += 1
            case Verdict.BENIGN:             bd.benign += 1
            case _:                          bd.unknown += 1
    return bd


def compute_cost_summary(results: Sequence[AnalysisResult]) -> CostSummary:
    total = sum(r.cost_usd for r in results)
    tokens = sum(r.total_tokens for r in results)
    by_model: dict[str, float] = {}
    for r in results:
        by_model[r.model_id] = by_model.get(r.model_id, 0.0) + r.cost_usd
    return CostSummary(
        total_usd=total,
        total_tokens=tokens,
        avg_cost_per_alert=total / len(results) if results else 0.0,
        by_model=by_model,
    )


def compute_accuracy(feedback: Sequence[FeedbackRecord]) -> AccuracyMetrics:
    if not feedback:
        return AccuracyMetrics()

    n = len(feedback)
    correct = sum(1 for f in feedback if f.ai_verdict_was_correct)
    tp = sum(1 for f in feedback if f.analyst_verdict == "true_positive" and f.ai_verdict_was_correct)
    fp = sum(1 for f in feedback if f.analyst_verdict == "false_positive" and not f.ai_verdict_was_correct)
    fn = sum(1 for f in feedback if f.analyst_verdict == "true_positive" and not f.ai_verdict_was_correct)

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    return AccuracyMetrics(
        overall_accuracy=correct / n,
        precision=precision,
        recall=recall,
        f1=f1,
        sample_size=n,
    )


def compute_client_metrics(
    client_id: str,
    results: Sequence[AnalysisResult],
    feedback: Sequence[FeedbackRecord],
    *,
    window_hours: int = 24,
) -> ClientMetrics:
    now = datetime.utcnow()
    window_start = now - timedelta(hours=window_hours)
    return ClientMetrics(
        client_id=client_id,
        window_start=window_start,
        window_end=now,
        alert_count=len(results),
        verdict_breakdown=compute_verdict_breakdown(results),
        cost_summary=compute_cost_summary(results),
        accuracy=compute_accuracy(feedback),
        avg_latency_ms=sum(r.latency_ms for r in results) / len(results) if results else 0.0,
        avg_confidence=sum(r.confidence for r in results) / len(results) if results else 0.0,
    )
