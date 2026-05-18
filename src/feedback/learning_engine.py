"""Derives prompt hints and accuracy statistics from accumulated feedback."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alerttriage.src.core.alert_models import FeedbackRecord
from alerttriage.src.feedback.feedback_system import FeedbackSystem
from alerttriage.src.logger import get_logger

log = get_logger(__name__)


@dataclass
class RuleInsight:
    """Summary of AI performance for one detection rule."""

    rule_name: str
    total: int = 0
    correct: int = 0
    false_positive_rate: float = 0.0
    common_misclassifications: list[str] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


class LearningEngine:
    """
    Mines feedback records to:
      1. Identify rules where the AI consistently under- or over-fires.
      2. Generate natural-language hints injected into the system prompt.
      3. Surface the worst-performing rules for analyst review.
    """

    def __init__(self, feedback_system: FeedbackSystem) -> None:
        self.feedback = feedback_system

    def compute_rule_insights(self) -> list[RuleInsight]:
        records = self.feedback.get_for_client(limit=5000)
        by_rule: dict[str, list[FeedbackRecord]] = defaultdict(list)
        for r in records:
            rule = r.metadata.get("rule_name", "unknown")
            by_rule[rule].append(r)

        insights = []
        for rule_name, items in by_rule.items():
            correct = sum(1 for i in items if i.ai_verdict_was_correct)
            fp = sum(1 for i in items if i.analyst_verdict == "false_positive")
            misclassifications = [
                i.analyst_verdict
                for i in items
                if not i.ai_verdict_was_correct
            ][:5]
            insights.append(
                RuleInsight(
                    rule_name=rule_name,
                    total=len(items),
                    correct=correct,
                    false_positive_rate=fp / len(items) if items else 0.0,
                    common_misclassifications=misclassifications,
                )
            )
        return sorted(insights, key=lambda x: x.accuracy)

    def generate_prompt_hints(self, *, max_hints: int = 10) -> str:
        """
        Returns a block of text to prepend to the system prompt.

        Example output:
          "Rule 'Brute Force Login' has a 78% false-positive rate for this client.
           Reason: internal vulnerability scanner triggers it nightly."
        """
        insights = self.compute_rule_insights()
        high_fp = [i for i in insights if i.false_positive_rate > 0.5 and i.total >= 5]

        if not high_fp:
            return ""

        lines = ["## Client-specific guidance (derived from analyst feedback)\n"]
        for ins in high_fp[:max_hints]:
            lines.append(
                f"- Rule '{ins.rule_name}': {ins.false_positive_rate:.0%} false-positive "
                f"rate ({ins.total} samples). Be conservative before marking as true_positive."
            )
        return "\n".join(lines)

    def worst_performing_rules(self, *, n: int = 5) -> list[RuleInsight]:
        return self.compute_rule_insights()[:n]

    def overall_accuracy(self) -> float:
        return self.feedback.accuracy()
