"""Derives prompt hints and accuracy statistics from accumulated feedback.

The engine is intentionally simple — no embeddings, no fine-tuning. It
runs aggregate SQL against the feedback store, finds rules where the AI
is consistently wrong, and turns each into a natural-language hint that
:class:`alerttriage.src.feedback.prompt_enhancer.PromptEnhancer` prepends
to the system prompt on the next run.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from alerttriage.src.feedback.feedback_system import FeedbackSystem
from alerttriage.src.logger import get_logger

log = get_logger(__name__)


@dataclass
class RuleInsight:
    """Aggregate AI performance for a single detection rule."""

    rule_name: str
    total: int = 0
    correct: int = 0
    false_positive_rate: float = 0.0
    common_misclassifications: list[str] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        """Fraction of analyses for this rule that the analyst agreed with."""
        return self.correct / self.total if self.total else 0.0


class LearningEngine:
    """Mines feedback records to surface guidance for the system prompt.

    Responsibilities:
      1. Identify rules where the AI consistently under- or over-fires.
      2. Generate natural-language hints injected into the system prompt.
      3. Surface the worst-performing rules for analyst dashboards.
    """

    def __init__(
        self,
        feedback_system: FeedbackSystem,
        *,
        fp_rate_threshold: float = 0.5,
        min_sample_size: int = 5,
    ) -> None:
        """Construct a learning engine.

        Args:
            feedback_system: Per-client feedback store.
            fp_rate_threshold: Rule must have an FP rate at or above this to
                earn a prompt hint.
            min_sample_size: Rule must have at least this many feedback rows
                before its FP rate is trusted enough to generate a hint.
        """
        self.feedback = feedback_system
        self._fp_threshold = fp_rate_threshold
        self._min_samples = min_sample_size

    def compute_rule_insights(self) -> list[RuleInsight]:
        """Return per-rule insights sorted by accuracy (worst first)."""
        insights: list[RuleInsight] = []
        for row in self.feedback.rule_stats():
            misclassifications = (
                self.feedback.recent_misclassifications(row.rule_name, limit=5)
                if row.total > row.correct
                else []
            )
            insights.append(
                RuleInsight(
                    rule_name=row.rule_name,
                    total=row.total,
                    correct=row.correct,
                    false_positive_rate=(row.false_positives / row.total if row.total else 0.0),
                    common_misclassifications=misclassifications,
                )
            )
        return sorted(insights, key=lambda x: x.accuracy)

    def generate_prompt_hints(self, *, max_hints: int = 10) -> str:
        """Return a block of text to prepend to the system prompt.

        Returns an empty string when no rule meets the configured thresholds.
        """
        if max_hints <= 0:
            return ""

        insights = self.compute_rule_insights()
        flagged = [
            i
            for i in insights
            if i.false_positive_rate >= self._fp_threshold and i.total >= self._min_samples
        ]
        if not flagged:
            return ""

        lines = ["## Client-specific guidance (derived from analyst feedback)"]
        for ins in flagged[:max_hints]:
            lines.append(
                f"- Rule '{ins.rule_name}': {ins.false_positive_rate:.0%} false-positive "
                f"rate ({ins.total} samples). Be conservative before marking as true_positive."
            )
        log.debug(
            "prompt_hints_generated",
            client=self.feedback.client_id,
            hint_count=len(flagged[:max_hints]),
        )
        return "\n".join(lines)

    def worst_performing_rules(self, *, n: int = 5) -> list[RuleInsight]:
        """Return the ``n`` worst rules by accuracy (ascending)."""
        return self.compute_rule_insights()[:n]

    def overall_accuracy(self) -> float:
        """Fraction of all analyses across the client that matched analyst verdict."""
        return self.feedback.accuracy()
