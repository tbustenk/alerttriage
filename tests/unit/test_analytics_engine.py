"""Unit tests for AnalyticsEngine."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from alerttriage.src.analytics.engine import AnalyticsEngine
from alerttriage.src.core.alert_models import FeedbackRecord
from alerttriage.src.feedback.feedback_system import FeedbackSystem

CLIENT_ID = "test-client"


def _make_record(
    *,
    i: int = 0,
    rule: str = "BruteForce",
    correct: bool = True,
    verdict: str | None = None,
    days_ago: int = 0,
) -> FeedbackRecord:
    return FeedbackRecord(
        alert_id=f"alert-{i}-{rule}",
        analysis_id=f"analysis-{i}",
        client_id=CLIENT_ID,
        analyst_id="analyst",
        analyst_verdict=verdict or ("true_positive" if correct else "false_positive"),
        ai_verdict_was_correct=correct,
        metadata={"rule_name": rule},
    )


class _SimpleFakeConfig:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def get_client(self, client_id: str) -> dict:
        return {}

    def list_clients(self) -> list[str]:
        return [CLIENT_ID]


@pytest.fixture
def engine(tmp_data_dir):
    """AnalyticsEngine backed by a real FeedbackSystem with seeded data."""
    fs = FeedbackSystem(tmp_data_dir, CLIENT_ID)
    records = (
        [_make_record(i=i, rule="PortScan", correct=True) for i in range(10)]
        + [_make_record(i=100 + i, rule="BruteForce", correct=False, verdict="false_positive") for i in range(5)]
        + [_make_record(i=200 + i, rule="Ransomware", correct=True) for i in range(3)]
    )
    fs.record_many(records)
    return AnalyticsEngine(tmp_data_dir, _SimpleFakeConfig(tmp_data_dir))


class TestWeeklySummary:
    def test_returns_summary_with_data(self, engine):
        summary = engine.compute_weekly_summary(CLIENT_ID, weeks=52)
        assert summary.client_id == CLIENT_ID
        assert summary.total_alerts == 18
        assert summary.true_positives == 13
        assert summary.false_positives == 5

    def test_overall_accuracy(self, engine):
        summary = engine.compute_weekly_summary(CLIENT_ID, weeks=52)
        assert summary.overall_accuracy == pytest.approx(13 / 18, abs=0.01)

    def test_labor_value_positive_when_correct(self, engine):
        summary = engine.compute_weekly_summary(
            CLIENT_ID, weeks=52, analyst_hourly_rate=100.0, analyst_minutes_per_alert=60.0
        )
        # 13 correct × (60/60) hr × $100 = $1300 labor
        assert summary.labor_value_usd == pytest.approx(1300.0, abs=1.0)

    def test_empty_db_returns_zero_summary(self, tmp_data_dir):
        engine = AnalyticsEngine(tmp_data_dir, _SimpleFakeConfig(tmp_data_dir))
        summary = engine.compute_weekly_summary(CLIENT_ID)
        assert summary.total_alerts == 0
        assert summary.overall_accuracy == 0.0

    def test_net_value_equals_labor_minus_cost(self, engine):
        s = engine.compute_weekly_summary(CLIENT_ID, weeks=52)
        assert s.net_value_usd == pytest.approx(s.labor_value_usd - s.ai_cost_usd, abs=0.01)

    def test_weeks_list_populated(self, engine):
        s = engine.compute_weekly_summary(CLIENT_ID, weeks=52)
        assert len(s.weeks) >= 1
        for w in s.weeks:
            assert w.total > 0
            assert 0.0 <= w.accuracy <= 1.0


class TestAlertTypeBreakdown:
    def test_rule_count(self, engine):
        bd = engine.compute_alert_type_breakdown(CLIENT_ID, days=365)
        rules = {r.rule_name for r in bd.rules}
        assert "PortScan" in rules
        assert "BruteForce" in rules

    def test_fp_rate_for_bruteforce(self, engine):
        bd = engine.compute_alert_type_breakdown(CLIENT_ID, days=365)
        bf = next(r for r in bd.rules if r.rule_name == "BruteForce")
        assert bf.fp_rate == pytest.approx(1.0, abs=0.01)  # 5/5 = 100% FP

    def test_accuracy_for_portscan(self, engine):
        bd = engine.compute_alert_type_breakdown(CLIENT_ID, days=365)
        ps = next(r for r in bd.rules if r.rule_name == "PortScan")
        assert ps.accuracy == pytest.approx(1.0, abs=0.01)

    def test_most_problematic_is_bruteforce(self, engine):
        bd = engine.compute_alert_type_breakdown(CLIENT_ID, days=365)
        assert "BruteForce" in bd.most_problematic

    def test_most_common_is_portscan(self, engine):
        bd = engine.compute_alert_type_breakdown(CLIENT_ID, days=365)
        assert "PortScan" in bd.most_common


class TestROIReport:
    def test_roi_fields_present(self, engine):
        roi = engine.compute_roi(CLIENT_ID, days=365, analyst_hourly_rate=75.0, analyst_minutes_per_alert=15.0)
        assert roi.alerts_analyzed == 18
        assert roi.hours_saved >= 0
        assert roi.labor_value_usd >= 0
        assert roi.net_roi_usd == pytest.approx(roi.labor_value_usd - roi.ai_cost_usd, abs=0.01)

    def test_payback_ratio(self, engine):
        roi = engine.compute_roi(CLIENT_ID, days=365, analyst_hourly_rate=100.0)
        if roi.ai_cost_usd > 0:
            expected = roi.labor_value_usd / roi.ai_cost_usd
            assert roi.payback_ratio == pytest.approx(expected, rel=0.01)

    def test_empty_db_roi(self, tmp_data_dir):
        engine = AnalyticsEngine(tmp_data_dir, _SimpleFakeConfig(tmp_data_dir))
        roi = engine.compute_roi(CLIENT_ID)
        assert roi.alerts_analyzed == 0
        assert roi.hours_saved == 0.0


class TestFPAnalysis:
    def test_total_fp_count(self, engine):
        fp = engine.compute_fp_analysis(CLIENT_ID, days=365)
        assert fp.total_false_positives == 5

    def test_worst_rules_includes_bruteforce(self, engine):
        fp = engine.compute_fp_analysis(CLIENT_ID, days=365)
        assert "BruteForce" in fp.worst_rules

    def test_recommendations_is_list(self, engine):
        fp = engine.compute_fp_analysis(CLIENT_ID, days=365)
        assert isinstance(fp.recommendations, list)


class TestTrendReport:
    def test_trend_fields(self, engine):
        tr = engine.compute_trends(CLIENT_ID, days=90)
        assert tr.client_id == CLIENT_ID
        assert tr.accuracy_direction in ("improving", "stable", "declining", "insufficient_data")
        assert isinstance(tr.accuracy_trend, list)
        assert isinstance(tr.throughput_trend, list)

    def test_empty_trend(self, tmp_data_dir):
        engine = AnalyticsEngine(tmp_data_dir, _SimpleFakeConfig(tmp_data_dir))
        tr = engine.compute_trends(CLIENT_ID)
        assert tr.accuracy_direction in ("stable", "insufficient_data")


class TestClientReport:
    def test_generates_full_report(self, engine):
        report = engine.generate_client_report(CLIENT_ID, period_days=365)
        assert report.client_id == CLIENT_ID
        assert report.weekly_summary.total_alerts == 18
        assert report.alert_type_breakdown is not None
        assert report.fp_analysis is not None
        assert report.roi_report is not None
        assert report.trend_report is not None
        assert isinstance(report.executive_summary, str)
        assert len(report.executive_summary) > 0

    def test_to_dict_serialisable(self, engine):
        import json

        report = engine.generate_client_report(CLIENT_ID, period_days=365)
        d = engine.to_dict(report)
        # Should be JSON serialisable
        json.dumps(d)
        assert "client_id" in d
