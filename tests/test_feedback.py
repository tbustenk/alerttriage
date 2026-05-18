"""Unit tests for FeedbackSystem and LearningEngine."""

import tempfile
from pathlib import Path

import pytest

from alerttriage.src.core.alert_models import FeedbackRecord
from alerttriage.src.feedback.feedback_system import FeedbackSystem
from alerttriage.src.feedback.learning_engine import LearningEngine


def _make_feedback(client_id="test", correct=True, rule="Test Rule") -> FeedbackRecord:
    return FeedbackRecord(
        alert_id="alert-1",
        analysis_id="analysis-1",
        client_id=client_id,
        analyst_id="analyst-1",
        analyst_verdict="true_positive" if correct else "false_positive",
        ai_verdict_was_correct=correct,
        metadata={"rule_name": rule},
    )


class TestFeedbackSystem:
    def setup_method(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.fs = FeedbackSystem(Path(self._tmpdir.name), "test")

    def teardown_method(self):
        self._tmpdir.cleanup()

    def test_record_and_retrieve(self):
        fb = _make_feedback()
        self.fs.record(fb)
        records = self.fs.get_for_client()
        assert len(records) == 1
        assert records[0].id == fb.id

    def test_accuracy_all_correct(self):
        for _ in range(5):
            self.fs.record(_make_feedback(correct=True))
        assert self.fs.accuracy() == 1.0

    def test_accuracy_mixed(self):
        self.fs.record(_make_feedback(correct=True))
        self.fs.record(_make_feedback(correct=False))
        assert self.fs.accuracy() == pytest.approx(0.5)

    def test_empty_accuracy(self):
        assert self.fs.accuracy() == 0.0


class TestLearningEngine:
    def setup_method(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.fs = FeedbackSystem(Path(self._tmpdir.name), "test")
        self.engine = LearningEngine(self.fs)

    def teardown_method(self):
        self._tmpdir.cleanup()

    def test_no_hints_when_empty(self):
        assert self.engine.generate_prompt_hints() == ""

    def test_prompt_hints_for_high_fp_rule(self):
        for _ in range(6):
            self.fs.record(_make_feedback(correct=False, rule="Brute Force"))
        hints = self.engine.generate_prompt_hints()
        assert "Brute Force" in hints
