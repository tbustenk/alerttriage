"""Unit tests for FeedbackSystem and LearningEngine."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from alerttriage.src.core.alert_models import FeedbackRecord
from alerttriage.src.feedback.feedback_system import FeedbackSystem
from alerttriage.src.feedback.learning_engine import LearningEngine


def _make_feedback(
    *,
    client_id: str = "test",
    correct: bool = True,
    rule: str = "Test Rule",
    verdict: str | None = None,
) -> FeedbackRecord:
    return FeedbackRecord(
        alert_id="alert-1",
        analysis_id="analysis-1",
        client_id=client_id,
        analyst_id="analyst-1",
        analyst_verdict=verdict or ("true_positive" if correct else "false_positive"),
        ai_verdict_was_correct=correct,
        metadata={"rule_name": rule},
    )


class TestFeedbackSystem:
    def setup_method(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.fs = FeedbackSystem(Path(self._tmpdir.name), "test")

    def teardown_method(self) -> None:
        self._tmpdir.cleanup()

    def test_record_and_retrieve(self) -> None:
        fb = _make_feedback()
        self.fs.record(fb)
        records = self.fs.get_for_client()
        assert len(records) == 1
        assert records[0].id == fb.id
        assert records[0].metadata["rule_name"] == "Test Rule"

    def test_record_many_in_one_transaction(self) -> None:
        batch = [_make_feedback(rule=f"Rule {i}") for i in range(50)]
        written = self.fs.record_many(batch)
        assert written == 50
        assert len(self.fs.get_for_client(limit=100)) == 50

    def test_record_many_empty(self) -> None:
        assert self.fs.record_many([]) == 0

    def test_accuracy_all_correct(self) -> None:
        self.fs.record_many([_make_feedback(correct=True) for _ in range(5)])
        assert self.fs.accuracy() == 1.0

    def test_accuracy_mixed(self) -> None:
        self.fs.record(_make_feedback(correct=True))
        self.fs.record(_make_feedback(correct=False))
        assert self.fs.accuracy() == pytest.approx(0.5)

    def test_empty_accuracy(self) -> None:
        assert self.fs.accuracy() == 0.0

    def test_rule_stats_aggregation(self) -> None:
        self.fs.record_many(
            [_make_feedback(rule="Login", correct=False) for _ in range(3)]
            + [_make_feedback(rule="Login", correct=True) for _ in range(1)]
            + [_make_feedback(rule="Recon", correct=True) for _ in range(2)]
        )
        stats = {row.rule_name: row for row in self.fs.rule_stats()}
        assert stats["Login"].total == 4
        assert stats["Login"].correct == 1
        assert stats["Login"].false_positives == 3
        assert stats["Recon"].total == 2

    def test_recovers_from_corrupt_db(self) -> None:
        # Open and close cleanly, then truncate the file to garbage.
        self.fs.record(_make_feedback())
        db_path = Path(self._tmpdir.name) / "test" / "feedback.db"
        db_path.write_bytes(b"this is not sqlite")
        # New instance should archive the corrupt file and start fresh.
        fresh = FeedbackSystem(Path(self._tmpdir.name), "test")
        assert fresh.accuracy() == 0.0
        backups = list(db_path.parent.glob("feedback.corrupt-*"))
        assert backups, "expected the corrupt DB to be archived"


class TestLearningEngine:
    def setup_method(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.fs = FeedbackSystem(Path(self._tmpdir.name), "test")
        self.engine = LearningEngine(self.fs, fp_rate_threshold=0.5, min_sample_size=5)

    def teardown_method(self) -> None:
        self._tmpdir.cleanup()

    def test_no_hints_when_empty(self) -> None:
        assert self.engine.generate_prompt_hints() == ""

    def test_no_hints_under_sample_threshold(self) -> None:
        # 2 samples is below min_sample_size=5
        self.fs.record_many([_make_feedback(correct=False, rule="Sparse") for _ in range(2)])
        assert self.engine.generate_prompt_hints() == ""

    def test_no_hints_when_fp_rate_low(self) -> None:
        self.fs.record_many(
            [_make_feedback(correct=True, rule="Good", verdict="true_positive") for _ in range(10)]
        )
        assert self.engine.generate_prompt_hints() == ""

    def test_prompt_hints_for_high_fp_rule(self) -> None:
        self.fs.record_many([_make_feedback(correct=False, rule="Brute Force") for _ in range(6)])
        hints = self.engine.generate_prompt_hints()
        assert "Brute Force" in hints
        assert "100% false-positive" in hints

    def test_max_hints_caps_output(self) -> None:
        for i in range(12):
            self.fs.record_many([_make_feedback(correct=False, rule=f"Rule-{i}") for _ in range(5)])
        hints = self.engine.generate_prompt_hints(max_hints=3)
        # At most 3 bullet lines + 1 header
        assert sum(1 for line in hints.splitlines() if line.startswith("-")) == 3

    def test_max_hints_zero_returns_empty(self) -> None:
        self.fs.record_many([_make_feedback(correct=False) for _ in range(6)])
        assert self.engine.generate_prompt_hints(max_hints=0) == ""

    def test_worst_performing_rules_orders_by_accuracy(self) -> None:
        self.fs.record_many(
            [_make_feedback(correct=False, rule="Bad") for _ in range(5)]
            + [_make_feedback(correct=True, rule="Ok") for _ in range(5)]
        )
        worst = self.engine.worst_performing_rules(n=2)
        assert worst[0].rule_name == "Bad"
