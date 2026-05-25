"""Unit tests for PromptEnhancer."""

from __future__ import annotations

import tempfile
from pathlib import Path

from alerttriage.src.core.alert_models import FeedbackRecord
from alerttriage.src.feedback.feedback_system import FeedbackSystem
from alerttriage.src.feedback.learning_engine import LearningEngine
from alerttriage.src.feedback.prompt_enhancer import PromptEnhancer


def _fb(rule: str = "Bad", correct: bool = False) -> FeedbackRecord:
    return FeedbackRecord(
        alert_id="a",
        analysis_id="b",
        client_id="acme",
        analyst_id="x",
        analyst_verdict="false_positive" if not correct else "true_positive",
        ai_verdict_was_correct=correct,
        metadata={"rule_name": rule},
    )


def _factory(store: FeedbackSystem):
    return lambda _client_id: LearningEngine(store, fp_rate_threshold=0.5, min_sample_size=5)


def test_returns_base_prompt_when_no_feedback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fs = FeedbackSystem(Path(tmp), "acme")
        enh = PromptEnhancer(_factory(fs), cache_ttl_sec=0)
        assert enh.enhance("BASE", client_id="acme") == "BASE"


def test_prepends_hints_when_high_fp_rule_exists() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fs = FeedbackSystem(Path(tmp), "acme")
        fs.record_many([_fb(rule="Brute Force") for _ in range(6)])
        enh = PromptEnhancer(_factory(fs), cache_ttl_sec=0)
        out = enh.enhance("BASE", client_id="acme")
        assert "Brute Force" in out
        assert out.endswith("BASE")


def test_cache_returns_same_hints_within_ttl() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fs = FeedbackSystem(Path(tmp), "acme")
        fs.record_many([_fb(rule="Brute Force") for _ in range(6)])
        enh = PromptEnhancer(_factory(fs), cache_ttl_sec=60)
        first = enh.enhance("BASE", client_id="acme")

        # Adding new feedback should NOT be reflected — cache TTL hasn't expired.
        fs.record_many([_fb(rule="New Rule") for _ in range(6)])
        second = enh.enhance("BASE", client_id="acme")
        assert first == second


def test_invalidate_forces_refresh() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fs = FeedbackSystem(Path(tmp), "acme")
        fs.record_many([_fb(rule="Brute Force") for _ in range(6)])
        enh = PromptEnhancer(_factory(fs), cache_ttl_sec=60)
        before = enh.enhance("BASE", client_id="acme")
        fs.record_many([_fb(rule="New Rule") for _ in range(6)])
        enh.invalidate("acme")
        after = enh.enhance("BASE", client_id="acme")
        assert "New Rule" in after
        assert before != after


def test_engine_failure_degrades_to_base_prompt() -> None:
    def broken_factory(client_id: str) -> LearningEngine:
        raise RuntimeError("DB unreachable")

    enh = PromptEnhancer(broken_factory, cache_ttl_sec=0)
    assert enh.enhance("BASE", client_id="acme") == "BASE"
