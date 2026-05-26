"""AnalyticsEngine — business-value analytics for AlertTriage.

Queries the per-client SQLite feedback DB directly (read-only) and the
CostController state files to produce:

  - Weekly summaries (throughput, accuracy, cost)
  - Alert-type breakdown (which rule types cause the most work)
  - False-positive pattern analysis (root causes, recommendations)
  - ROI calculation (hours saved × analyst hourly rate − AI cost)
  - Trend analysis (accuracy, FP rate, throughput over time)
  - Bundled ClientReport for export to PDF / CSV / HTML
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

from alerttriage.src.logger import get_logger

if TYPE_CHECKING:
    from alerttriage.config.config_manager import ConfigManager

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class WeekData:
    """Metrics for one calendar week."""

    week: str           # ISO week: YYYY-WNN
    total: int
    correct: int
    false_positives: int
    accuracy: float


@dataclass
class WeeklySummary:
    client_id: str
    generated_at: datetime
    period_weeks: int
    weeks: list[WeekData]
    total_alerts: int
    overall_accuracy: float
    true_positives: int
    false_positives: int
    estimated_hours_saved: float
    analyst_hourly_rate: float
    labor_value_usd: float
    ai_cost_usd: float
    net_value_usd: float


@dataclass
class RuleAnalytics:
    rule_name: str
    total: int
    correct: int
    false_positives: int
    fp_rate: float
    accuracy: float
    trend: str  # improving | stable | worsening | insufficient_data


@dataclass
class AlertTypeBreakdown:
    client_id: str
    period_days: int
    generated_at: datetime
    rules: list[RuleAnalytics]
    most_problematic: list[str]   # highest FP rate (≥5 samples)
    most_common: list[str]        # highest total volume


@dataclass
class FPPattern:
    description: str
    count: int
    rules: list[str]


@dataclass
class FPAnalysis:
    client_id: str
    generated_at: datetime
    period_days: int
    total_false_positives: int
    fp_rate: float
    worst_rules: list[str]
    patterns: list[FPPattern]
    recommendations: list[str]


@dataclass
class ROIReport:
    client_id: str
    generated_at: datetime
    period_days: int
    alerts_analyzed: int
    analyst_minutes_per_alert: float
    analyst_hourly_rate: float
    hours_saved: float
    labor_value_usd: float
    ai_cost_usd: float
    net_roi_usd: float
    roi_percentage: float
    payback_ratio: float        # labor_value / ai_cost
    monthly_projection_usd: float


@dataclass
class TrendDataPoint:
    period: str     # ISO week (YYYY-WNN) or month (YYYY-MM)
    value: float
    count: int = 0


@dataclass
class TrendReport:
    client_id: str
    generated_at: datetime
    period_days: int
    accuracy_trend: list[TrendDataPoint]
    throughput_trend: list[TrendDataPoint]
    fp_rate_trend: list[TrendDataPoint]
    accuracy_direction: str     # improving | stable | declining
    accuracy_change_pct: float  # delta between first and last complete week


@dataclass
class ClientReport:
    """Full analytics bundle for one client — exported to PDF/CSV/HTML."""

    client_id: str
    generated_at: datetime
    period_days: int
    weekly_summary: WeeklySummary
    alert_type_breakdown: AlertTypeBreakdown
    fp_analysis: FPAnalysis
    roi_report: ROIReport
    trend_report: TrendReport
    executive_summary: str


# ---------------------------------------------------------------------------
# AnalyticsEngine
# ---------------------------------------------------------------------------


class AnalyticsEngine:
    """Produce business-value analytics for a client from historical feedback.

    Args:
        data_dir: Root data directory (contains ``<client_id>/feedback.db``).
        config:   Validated :class:`ConfigManager` (used for cost data).
    """

    DEFAULT_ANALYST_MINUTES: float = 15.0   # industry average per alert
    DEFAULT_HOURLY_RATE: float = 75.0       # USD — typical SOC analyst L1

    def __init__(self, data_dir: Path, config: ConfigManager) -> None:
        self._data_dir = Path(data_dir)
        self._config = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_weekly_summary(
        self,
        client_id: str,
        *,
        weeks: int = 4,
        analyst_hourly_rate: float = DEFAULT_HOURLY_RATE,
        analyst_minutes_per_alert: float = DEFAULT_ANALYST_MINUTES,
    ) -> WeeklySummary:
        """Return weekly performance and ROI summary for the last *weeks* weeks."""
        since = (datetime.now(timezone.utc) - timedelta(weeks=weeks)).isoformat()
        week_rows = self._query(
            client_id,
            """
            SELECT
                strftime('%Y-W%W', timestamp)       AS week,
                COUNT(*)                            AS total,
                COALESCE(SUM(ai_correct), 0)        AS correct,
                COALESCE(SUM(CASE WHEN analyst_verdict='false_positive'
                                  THEN 1 ELSE 0 END), 0) AS fps
            FROM feedback
            WHERE client_id=? AND timestamp >= ?
            GROUP BY week ORDER BY week
            """,
            (client_id, since),
        )

        weeks_data: list[WeekData] = []
        total_alerts = total_correct = total_fps = 0
        for r in week_rows:
            t, c, fp = int(r["total"]), int(r["correct"]), int(r["fps"])
            weeks_data.append(WeekData(
                week=r["week"],
                total=t,
                correct=c,
                false_positives=fp,
                accuracy=c / t if t else 0.0,
            ))
            total_alerts += t
            total_correct += c
            total_fps += fp

        overall_accuracy = total_correct / total_alerts if total_alerts else 0.0
        hours_saved = total_correct * (analyst_minutes_per_alert / 60)
        labor_value = round(hours_saved * analyst_hourly_rate, 2)
        ai_cost = self._estimate_period_cost(client_id, days=weeks * 7)
        net_value = round(labor_value - ai_cost, 2)

        return WeeklySummary(
            client_id=client_id,
            generated_at=datetime.now(timezone.utc),
            period_weeks=weeks,
            weeks=weeks_data,
            total_alerts=total_alerts,
            overall_accuracy=overall_accuracy,
            true_positives=total_alerts - total_fps,
            false_positives=total_fps,
            estimated_hours_saved=round(hours_saved, 2),
            analyst_hourly_rate=analyst_hourly_rate,
            labor_value_usd=labor_value,
            ai_cost_usd=ai_cost,
            net_value_usd=net_value,
        )

    def compute_alert_type_breakdown(
        self,
        client_id: str,
        *,
        days: int = 30,
    ) -> AlertTypeBreakdown:
        """Return per-rule-name statistics for the last *days* days."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self._query(
            client_id,
            """
            SELECT
                rule_name,
                COUNT(*)                            AS total,
                COALESCE(SUM(ai_correct), 0)        AS correct,
                COALESCE(SUM(CASE WHEN analyst_verdict='false_positive'
                                  THEN 1 ELSE 0 END), 0) AS fps
            FROM feedback
            WHERE client_id=? AND timestamp >= ?
            GROUP BY rule_name ORDER BY total DESC
            """,
            (client_id, since),
        )

        # For trend: compare first half vs second half of period
        half_since = (datetime.now(timezone.utc) - timedelta(days=days // 2)).isoformat()
        recent_rows = self._query(
            client_id,
            """
            SELECT rule_name,
                   COALESCE(SUM(CASE WHEN analyst_verdict='false_positive'
                                     THEN 1 ELSE 0 END), 0) * 1.0 / MAX(COUNT(*), 1) AS recent_fp_rate
            FROM feedback
            WHERE client_id=? AND timestamp >= ?
            GROUP BY rule_name
            """,
            (client_id, half_since),
        )
        recent_fp_map = {r["rule_name"]: r["recent_fp_rate"] for r in recent_rows}

        rules: list[RuleAnalytics] = []
        for r in rows:
            t, c, fp = int(r["total"]), int(r["correct"]), int(r["fps"])
            fp_rate = fp / t if t else 0.0
            recent_fp = recent_fp_map.get(r["rule_name"], fp_rate)
            if t < 3:
                trend = "insufficient_data"
            elif recent_fp < fp_rate - 0.05:
                trend = "improving"
            elif recent_fp > fp_rate + 0.05:
                trend = "worsening"
            else:
                trend = "stable"
            rules.append(RuleAnalytics(
                rule_name=r["rule_name"],
                total=t,
                correct=c,
                false_positives=fp,
                fp_rate=fp_rate,
                accuracy=c / t if t else 0.0,
                trend=trend,
            ))

        problematic = sorted(
            (r for r in rules if r.total >= 5),
            key=lambda x: x.fp_rate,
            reverse=True,
        )
        most_common = sorted(rules, key=lambda x: x.total, reverse=True)

        return AlertTypeBreakdown(
            client_id=client_id,
            period_days=days,
            generated_at=datetime.now(timezone.utc),
            rules=rules,
            most_problematic=[r.rule_name for r in problematic[:5]],
            most_common=[r.rule_name for r in most_common[:5]],
        )

    def compute_fp_analysis(
        self,
        client_id: str,
        *,
        days: int = 30,
    ) -> FPAnalysis:
        """Analyse false-positive patterns and surface recommendations."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        totals = self._query(
            client_id,
            "SELECT COUNT(*) AS total, SUM(CASE WHEN analyst_verdict='false_positive' THEN 1 ELSE 0 END) AS fps FROM feedback WHERE client_id=? AND timestamp >= ?",
            (client_id, since),
        )
        total = int((totals[0]["total"] if totals else 0) or 0)
        total_fps = int((totals[0]["fps"] if totals else 0) or 0)
        fp_rate = total_fps / total if total else 0.0

        # Top FP rules
        rule_rows = self._query(
            client_id,
            """
            SELECT rule_name,
                   SUM(CASE WHEN analyst_verdict='false_positive' THEN 1 ELSE 0 END) AS fps,
                   COUNT(*) AS total
            FROM feedback WHERE client_id=? AND timestamp >= ?
            GROUP BY rule_name HAVING fps > 0
            ORDER BY fps DESC LIMIT 10
            """,
            (client_id, since),
        )
        worst_rules = [r["rule_name"] for r in rule_rows[:5]]

        # Pattern analysis from analyst notes
        notes_rows = self._query(
            client_id,
            """
            SELECT analyst_notes, rule_name
            FROM feedback
            WHERE client_id=? AND analyst_verdict='false_positive'
              AND analyst_notes != '' AND timestamp >= ?
            """,
            (client_id, since),
        )

        patterns = self._extract_fp_patterns(notes_rows)
        recommendations = self._generate_fp_recommendations(rule_rows, fp_rate, total)

        return FPAnalysis(
            client_id=client_id,
            generated_at=datetime.now(timezone.utc),
            period_days=days,
            total_false_positives=total_fps,
            fp_rate=fp_rate,
            worst_rules=worst_rules,
            patterns=patterns,
            recommendations=recommendations,
        )

    def compute_roi(
        self,
        client_id: str,
        *,
        days: int = 30,
        analyst_hourly_rate: float = DEFAULT_HOURLY_RATE,
        analyst_minutes_per_alert: float = DEFAULT_ANALYST_MINUTES,
    ) -> ROIReport:
        """Calculate return-on-investment for AI triage over *days*."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        row = self._query(
            client_id,
            "SELECT COUNT(*) AS total, COALESCE(SUM(ai_correct), 0) AS correct FROM feedback WHERE client_id=? AND timestamp >= ?",
            (client_id, since),
        )
        total = int((row[0]["total"] if row else 0) or 0)
        correct = int((row[0]["correct"] if row else 0) or 0)

        hours_saved = correct * (analyst_minutes_per_alert / 60)
        labor_value = round(hours_saved * analyst_hourly_rate, 2)
        ai_cost = self._estimate_period_cost(client_id, days=days)
        net_roi = round(labor_value - ai_cost, 2)
        roi_pct = (net_roi / ai_cost * 100) if ai_cost > 0 else (999.9 if net_roi > 0 else 0.0)
        payback = (labor_value / ai_cost) if ai_cost > 0 else 0.0
        monthly_projection = round(net_roi * (30 / max(days, 1)), 2)

        return ROIReport(
            client_id=client_id,
            generated_at=datetime.now(timezone.utc),
            period_days=days,
            alerts_analyzed=total,
            analyst_minutes_per_alert=analyst_minutes_per_alert,
            analyst_hourly_rate=analyst_hourly_rate,
            hours_saved=round(hours_saved, 2),
            labor_value_usd=labor_value,
            ai_cost_usd=ai_cost,
            net_roi_usd=net_roi,
            roi_percentage=round(roi_pct, 1),
            payback_ratio=round(payback, 2),
            monthly_projection_usd=monthly_projection,
        )

    def compute_trends(
        self,
        client_id: str,
        *,
        days: int = 90,
    ) -> TrendReport:
        """Return weekly accuracy, FP-rate, and throughput trends."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self._query(
            client_id,
            """
            SELECT
                strftime('%Y-W%W', timestamp)       AS period,
                COUNT(*)                            AS total,
                COALESCE(AVG(ai_correct), 0)        AS accuracy,
                COALESCE(AVG(CASE WHEN analyst_verdict='false_positive'
                                  THEN 1.0 ELSE 0.0 END), 0) AS fp_rate
            FROM feedback
            WHERE client_id=? AND timestamp >= ?
            GROUP BY period ORDER BY period
            """,
            (client_id, since),
        )

        acc_trend = [TrendDataPoint(period=r["period"], value=round(float(r["accuracy"]), 4), count=int(r["total"])) for r in rows]
        fp_trend  = [TrendDataPoint(period=r["period"], value=round(float(r["fp_rate"]),  4), count=int(r["total"])) for r in rows]
        thru_trend= [TrendDataPoint(period=r["period"], value=float(r["total"]),            count=int(r["total"])) for r in rows]

        # Determine direction from first vs last 25% of data points
        direction = "stable"
        acc_change = 0.0
        complete = [p for p in acc_trend if p.count >= 3]
        if len(complete) >= 4:
            first_avg = sum(p.value for p in complete[:2]) / 2
            last_avg  = sum(p.value for p in complete[-2:]) / 2
            acc_change = round((last_avg - first_avg) * 100, 1)
            if acc_change >= 2.0:
                direction = "improving"
            elif acc_change <= -2.0:
                direction = "declining"

        return TrendReport(
            client_id=client_id,
            generated_at=datetime.now(timezone.utc),
            period_days=days,
            accuracy_trend=acc_trend,
            throughput_trend=thru_trend,
            fp_rate_trend=fp_trend,
            accuracy_direction=direction,
            accuracy_change_pct=acc_change,
        )

    def generate_client_report(
        self,
        client_id: str,
        *,
        period_days: int = 30,
        analyst_hourly_rate: float = DEFAULT_HOURLY_RATE,
        analyst_minutes_per_alert: float = DEFAULT_ANALYST_MINUTES,
    ) -> ClientReport:
        """Bundle all analytics into a single exportable report."""
        weeks = max(1, period_days // 7)
        weekly  = self.compute_weekly_summary(client_id, weeks=weeks, analyst_hourly_rate=analyst_hourly_rate, analyst_minutes_per_alert=analyst_minutes_per_alert)
        breakdown = self.compute_alert_type_breakdown(client_id, days=period_days)
        fp_analysis = self.compute_fp_analysis(client_id, days=period_days)
        roi  = self.compute_roi(client_id, days=period_days, analyst_hourly_rate=analyst_hourly_rate, analyst_minutes_per_alert=analyst_minutes_per_alert)
        trend = self.compute_trends(client_id, days=min(90, period_days * 3))

        summary = self._executive_summary(weekly, roi, trend)

        return ClientReport(
            client_id=client_id,
            generated_at=datetime.now(timezone.utc),
            period_days=period_days,
            weekly_summary=weekly,
            alert_type_breakdown=breakdown,
            fp_analysis=fp_analysis,
            roi_report=roi,
            trend_report=trend,
            executive_summary=summary,
        )

    def to_dict(self, report: ClientReport) -> dict[str, Any]:
        """Serialise a ClientReport to a JSON-safe dict."""
        def _dp(pts: list[TrendDataPoint]) -> list[dict[str, Any]]:
            return [{"period": p.period, "value": p.value, "count": p.count} for p in pts]

        ws = report.weekly_summary
        roi = report.roi_report
        fp = report.fp_analysis
        bd = report.alert_type_breakdown
        tr = report.trend_report

        return {
            "client_id": report.client_id,
            "generated_at": report.generated_at.isoformat(),
            "period_days": report.period_days,
            "executive_summary": report.executive_summary,
            "weekly_summary": {
                "period_weeks": ws.period_weeks,
                "total_alerts": ws.total_alerts,
                "overall_accuracy": ws.overall_accuracy,
                "true_positives": ws.true_positives,
                "false_positives": ws.false_positives,
                "hours_saved": ws.estimated_hours_saved,
                "labor_value_usd": ws.labor_value_usd,
                "ai_cost_usd": ws.ai_cost_usd,
                "net_value_usd": ws.net_value_usd,
                "weeks": [
                    {"week": w.week, "total": w.total, "correct": w.correct,
                     "false_positives": w.false_positives, "accuracy": w.accuracy}
                    for w in ws.weeks
                ],
            },
            "alert_type_breakdown": {
                "period_days": bd.period_days,
                "most_problematic": bd.most_problematic,
                "most_common": bd.most_common,
                "rules": [
                    {"rule": r.rule_name, "total": r.total, "fp_rate": r.fp_rate,
                     "accuracy": r.accuracy, "trend": r.trend}
                    for r in bd.rules
                ],
            },
            "fp_analysis": {
                "total_false_positives": fp.total_false_positives,
                "fp_rate": fp.fp_rate,
                "worst_rules": fp.worst_rules,
                "patterns": [{"description": p.description, "count": p.count, "rules": p.rules} for p in fp.patterns],
                "recommendations": fp.recommendations,
            },
            "roi": {
                "alerts_analyzed": roi.alerts_analyzed,
                "hours_saved": roi.hours_saved,
                "labor_value_usd": roi.labor_value_usd,
                "ai_cost_usd": roi.ai_cost_usd,
                "net_roi_usd": roi.net_roi_usd,
                "roi_percentage": roi.roi_percentage,
                "payback_ratio": roi.payback_ratio,
                "monthly_projection_usd": roi.monthly_projection_usd,
            },
            "trends": {
                "direction": tr.accuracy_direction,
                "accuracy_change_pct": tr.accuracy_change_pct,
                "accuracy": _dp(tr.accuracy_trend),
                "throughput": _dp(tr.throughput_trend),
                "fp_rate": _dp(tr.fp_rate_trend),
            },
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _query(self, client_id: str, sql: str, params: tuple) -> list[sqlite3.Row]:
        db_path = self._data_dir / client_id / "feedback.db"
        if not db_path.exists():
            return []
        try:
            conn = sqlite3.connect(str(db_path), timeout=5.0)
            conn.row_factory = sqlite3.Row
            try:
                return conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            log.error("analytics_query_failed", client=client_id, error=str(exc))
            return []

    def _estimate_period_cost(self, client_id: str, *, days: int) -> float:
        """Estimate AI API cost for a period from the CostController state file."""
        from alerttriage.src.cost_controller import CostController

        ctrl = CostController(self._config)
        spend = ctrl.get_spend(client_id)
        monthly = spend.get("monthly_usd", 0.0)
        return round(monthly * (days / 30), 4)

    def _extract_fp_patterns(self, notes_rows: list[sqlite3.Row]) -> list[FPPattern]:
        """Simple keyword clustering of analyst notes."""
        keyword_groups: dict[str, dict[str, Any]] = {
            "Known scanner / automated tool": {
                "keywords": ["scanner", "nessus", "qualys", "nmap", "scan", "automated", "tool"],
                "rules": set(),
                "count": 0,
            },
            "Internal / trusted IP": {
                "keywords": ["internal", "trusted", "whitelist", "allowlist", "known ip"],
                "rules": set(),
                "count": 0,
            },
            "Scheduled / maintenance activity": {
                "keywords": ["scheduled", "maintenance", "backup", "cron", "deploy", "patch"],
                "rules": set(),
                "count": 0,
            },
            "Dev / test environment": {
                "keywords": ["dev", "test", "staging", "qa", "sandbox", "lab"],
                "rules": set(),
                "count": 0,
            },
            "Expected user behaviour": {
                "keywords": ["expected", "normal", "baseline", "routine", "allowlist"],
                "rules": set(),
                "count": 0,
            },
        }

        for row in notes_rows:
            note = (row["analyst_notes"] or "").lower()
            rule = row["rule_name"]
            for label, grp in keyword_groups.items():
                if any(kw in note for kw in grp["keywords"]):
                    grp["count"] += 1
                    grp["rules"].add(rule)

        patterns = [
            FPPattern(description=label, count=grp["count"], rules=list(grp["rules"]))
            for label, grp in keyword_groups.items()
            if grp["count"] > 0
        ]
        return sorted(patterns, key=lambda p: p.count, reverse=True)

    def _generate_fp_recommendations(
        self,
        rule_rows: list[sqlite3.Row],
        fp_rate: float,
        total: int,
    ) -> list[str]:
        recs: list[str] = []

        if fp_rate > 0.4 and total > 10:
            recs.append(
                f"Overall FP rate is {fp_rate:.0%} — consider raising confidence thresholds "
                "or adding exclusion rules for known-safe entities."
            )

        for r in rule_rows[:3]:
            rule = r["rule_name"]
            fps = int(r["fps"])
            t = int(r["total"])
            if t >= 5 and fps / t > 0.5:
                recs.append(
                    f"Rule '{rule}' has a {fps/t:.0%} FP rate ({fps}/{t} alerts). "
                    "Add context filters or raise its severity threshold."
                )

        if not recs:
            if total < 20:
                recs.append("Collect more analyst feedback to surface actionable recommendations.")
            else:
                recs.append(
                    "FP rate is within acceptable bounds. Continue collecting feedback "
                    "to maintain learning accuracy."
                )

        return recs

    def _executive_summary(
        self,
        ws: WeeklySummary,
        roi: ROIReport,
        trend: TrendReport,
    ) -> str:
        acc_pct = f"{ws.overall_accuracy:.0%}"
        direction = trend.accuracy_direction
        dir_phrase = {
            "improving": "and accuracy is trending upward",
            "declining": "— note that accuracy has declined recently",
            "stable": "with stable accuracy",
        }.get(direction, "")

        if roi.net_roi_usd > 0:
            roi_phrase = (
                f"Over the past {roi.period_days} days AlertTriage analyzed "
                f"{roi.alerts_analyzed:,} alerts, saving an estimated "
                f"{roi.hours_saved:.0f} analyst hours and delivering "
                f"${roi.net_roi_usd:,.0f} in net value after AI costs."
            )
        else:
            roi_phrase = (
                f"AlertTriage processed {roi.alerts_analyzed:,} alerts over "
                f"{roi.period_days} days."
            )

        return (
            f"AI triage accuracy stands at {acc_pct} {dir_phrase}. "
            f"{roi_phrase} "
            f"Projected monthly net value: ${roi.monthly_projection_usd:,.0f}."
        )
