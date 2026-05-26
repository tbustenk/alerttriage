"""Analytics API router — GET /analytics/..."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from alerttriage.src.api.auth import require_api_key

router = APIRouter(prefix="/analytics", tags=["Analytics"])

# Analytics engine and exporter singletons are injected from app.py state.
# We import lazily to avoid circular imports.


def _engine() -> Any:
    from alerttriage.src.api.app import _analytics_engine  # type: ignore[attr-defined]
    if _analytics_engine is None:
        raise HTTPException(status_code=503, detail="Analytics engine not initialised")
    return _analytics_engine


def _reports_dir() -> Path:
    from alerttriage.src.api.app import _reports_dir as rd  # type: ignore[attr-defined]
    return rd


@router.get("/summary/{client_id}", summary="Weekly performance summary")
async def get_summary(
    client_id: str,
    weeks: int = Query(4, ge=1, le=52),
    hourly_rate: float = Query(75.0, ge=0),
    minutes_per_alert: float = Query(15.0, ge=1, le=120),
    _key: str = Depends(require_api_key),
) -> dict[str, Any]:
    """Weekly summary: throughput, accuracy, hours saved, labor value, AI cost, net ROI."""
    engine = _engine()
    summary = engine.compute_weekly_summary(
        client_id,
        weeks=weeks,
        analyst_hourly_rate=hourly_rate,
        analyst_minutes_per_alert=minutes_per_alert,
    )
    return {
        "client_id": summary.client_id,
        "generated_at": summary.generated_at.isoformat(),
        "period_weeks": summary.period_weeks,
        "total_alerts": summary.total_alerts,
        "overall_accuracy": summary.overall_accuracy,
        "true_positives": summary.true_positives,
        "false_positives": summary.false_positives,
        "estimated_hours_saved": summary.estimated_hours_saved,
        "analyst_hourly_rate": summary.analyst_hourly_rate,
        "labor_value_usd": summary.labor_value_usd,
        "ai_cost_usd": summary.ai_cost_usd,
        "net_value_usd": summary.net_value_usd,
        "weeks": [
            {"week": w.week, "total": w.total, "correct": w.correct,
             "false_positives": w.false_positives, "accuracy": w.accuracy}
            for w in summary.weeks
        ],
    }


@router.get("/breakdown/{client_id}", summary="Alert-type breakdown")
async def get_breakdown(
    client_id: str,
    days: int = Query(30, ge=1, le=365),
    _key: str = Depends(require_api_key),
) -> dict[str, Any]:
    """Per-rule statistics: FP rate, accuracy, trend, volume."""
    engine = _engine()
    bd = engine.compute_alert_type_breakdown(client_id, days=days)
    return {
        "client_id": bd.client_id,
        "period_days": bd.period_days,
        "generated_at": bd.generated_at.isoformat(),
        "most_problematic": bd.most_problematic,
        "most_common": bd.most_common,
        "rules": [
            {"rule": r.rule_name, "total": r.total, "correct": r.correct,
             "false_positives": r.false_positives, "fp_rate": r.fp_rate,
             "accuracy": r.accuracy, "trend": r.trend}
            for r in bd.rules
        ],
    }


@router.get("/fp-analysis/{client_id}", summary="False-positive pattern analysis")
async def get_fp_analysis(
    client_id: str,
    days: int = Query(30, ge=1, le=365),
    _key: str = Depends(require_api_key),
) -> dict[str, Any]:
    """Identify FP patterns from analyst notes and generate recommendations."""
    engine = _engine()
    fp = engine.compute_fp_analysis(client_id, days=days)
    return {
        "client_id": fp.client_id,
        "generated_at": fp.generated_at.isoformat(),
        "period_days": fp.period_days,
        "total_false_positives": fp.total_false_positives,
        "fp_rate": fp.fp_rate,
        "worst_rules": fp.worst_rules,
        "patterns": [{"description": p.description, "count": p.count, "rules": p.rules} for p in fp.patterns],
        "recommendations": fp.recommendations,
    }


@router.get("/roi/{client_id}", summary="ROI calculation")
async def get_roi(
    client_id: str,
    days: int = Query(30, ge=1, le=365),
    hourly_rate: float = Query(75.0, ge=0),
    minutes_per_alert: float = Query(15.0, ge=1, le=120),
    _key: str = Depends(require_api_key),
) -> dict[str, Any]:
    """Return on investment: labor saved, AI cost, net ROI, monthly projection."""
    engine = _engine()
    roi = engine.compute_roi(
        client_id,
        days=days,
        analyst_hourly_rate=hourly_rate,
        analyst_minutes_per_alert=minutes_per_alert,
    )
    return {
        "client_id": roi.client_id,
        "generated_at": roi.generated_at.isoformat(),
        "period_days": roi.period_days,
        "alerts_analyzed": roi.alerts_analyzed,
        "analyst_minutes_per_alert": roi.analyst_minutes_per_alert,
        "analyst_hourly_rate": roi.analyst_hourly_rate,
        "hours_saved": roi.hours_saved,
        "labor_value_usd": roi.labor_value_usd,
        "ai_cost_usd": roi.ai_cost_usd,
        "net_roi_usd": roi.net_roi_usd,
        "roi_percentage": roi.roi_percentage,
        "payback_ratio": roi.payback_ratio,
        "monthly_projection_usd": roi.monthly_projection_usd,
    }


@router.get("/trends/{client_id}", summary="Accuracy and throughput trends")
async def get_trends(
    client_id: str,
    days: int = Query(90, ge=7, le=365),
    _key: str = Depends(require_api_key),
) -> dict[str, Any]:
    """Weekly accuracy, FP-rate, and throughput trend data."""
    engine = _engine()
    tr = engine.compute_trends(client_id, days=days)
    return {
        "client_id": tr.client_id,
        "generated_at": tr.generated_at.isoformat(),
        "period_days": tr.period_days,
        "accuracy_direction": tr.accuracy_direction,
        "accuracy_change_pct": tr.accuracy_change_pct,
        "accuracy": [{"period": p.period, "value": p.value, "count": p.count} for p in tr.accuracy_trend],
        "fp_rate":  [{"period": p.period, "value": p.value, "count": p.count} for p in tr.fp_rate_trend],
        "throughput": [{"period": p.period, "value": p.value} for p in tr.throughput_trend],
    }


@router.get("/report/{client_id}", summary="Full analytics report (JSON)")
async def get_full_report(
    client_id: str,
    days: int = Query(30, ge=1, le=365),
    hourly_rate: float = Query(75.0, ge=0),
    minutes_per_alert: float = Query(15.0, ge=1, le=120),
    _key: str = Depends(require_api_key),
) -> dict[str, Any]:
    """Generate and return the full analytics bundle as JSON."""
    engine = _engine()
    report = engine.generate_client_report(
        client_id,
        period_days=days,
        analyst_hourly_rate=hourly_rate,
        analyst_minutes_per_alert=minutes_per_alert,
    )
    return engine.to_dict(report)


@router.get("/report/{client_id}/download", summary="Download report as PDF, HTML, or CSV", response_model=None)
async def download_report(
    client_id: str,
    format: str = Query("html", pattern="^(html|pdf|csv|json)$"),
    days: int = Query(30, ge=1, le=365),
    hourly_rate: float = Query(75.0, ge=0),
    minutes_per_alert: float = Query(15.0, ge=1, le=120),
    _key: str = Depends(require_api_key),
) -> StreamingResponse | FileResponse:
    """Download the full analytics report in the requested format."""
    from alerttriage.src.analytics.exporters import CSVExporter, HTMLExporter, JSONExporter, PDFExporter

    engine = _engine()
    reports_dir = _reports_dir() / client_id
    reports_dir.mkdir(parents=True, exist_ok=True)

    report = engine.generate_client_report(
        client_id,
        period_days=days,
        analyst_hourly_rate=hourly_rate,
        analyst_minutes_per_alert=minutes_per_alert,
    )

    if format == "pdf":
        try:
            path = PDFExporter(reports_dir).export(report)
            return FileResponse(path, media_type="application/pdf", filename=path.name)
        except ImportError:
            raise HTTPException(status_code=422, detail="fpdf2 not installed. Run: pip install fpdf2")

    if format == "csv":
        paths = CSVExporter(reports_dir).export(report)
        # Stream the primary (weekly) CSV
        content = paths[0].read_text(encoding="utf-8")
        return StreamingResponse(
            io.StringIO(content),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={paths[0].name}"},
        )

    if format == "json":
        path = JSONExporter(reports_dir).export(report, engine)
        return FileResponse(path, media_type="application/json", filename=path.name)

    # html (default)
    path = HTMLExporter(reports_dir).export(report)
    return FileResponse(path, media_type="text/html", filename=path.name)


# ── Schedule endpoints ─────────────────────────────────────────────────────

@router.get("/schedule/{client_id}", summary="Get report schedule")
async def get_schedule(
    client_id: str,
    _key: str = Depends(require_api_key),
) -> dict[str, Any]:
    from alerttriage.src.api.app import _report_scheduler  # type: ignore[attr-defined]
    if _report_scheduler is None:
        raise HTTPException(status_code=503, detail="Report scheduler not initialised")
    schedule = _report_scheduler.get_schedule(client_id)
    return schedule or {"message": f"No schedule configured for client '{client_id}'"}


@router.post("/schedule/{client_id}", status_code=201, summary="Create / update report schedule")
async def set_schedule(
    client_id: str,
    schedule: dict[str, Any],
    _key: str = Depends(require_api_key),
) -> dict[str, Any]:
    """
    Schedule automated reports.  ``schedule`` body:

    ```json
    {
      "frequency": "weekly",
      "day_of_week": 1,
      "hour": 9,
      "email": "soc@client.com",
      "format": "html",
      "analyst_hourly_rate": 75.0,
      "enabled": true
    }
    ```
    """
    from alerttriage.src.api.app import _report_scheduler  # type: ignore[attr-defined]
    if _report_scheduler is None:
        raise HTTPException(status_code=503, detail="Report scheduler not initialised")
    _report_scheduler.set_schedule(client_id, schedule)
    return {"status": "scheduled", "client_id": client_id, "schedule": schedule}


@router.delete("/schedule/{client_id}", summary="Delete report schedule")
async def delete_schedule(
    client_id: str,
    _key: str = Depends(require_api_key),
) -> dict[str, str]:
    from alerttriage.src.api.app import _report_scheduler  # type: ignore[attr-defined]
    if _report_scheduler is None:
        raise HTTPException(status_code=503, detail="Report scheduler not initialised")
    deleted = _report_scheduler.delete_schedule(client_id)
    return {"status": "deleted" if deleted else "not_found", "client_id": client_id}
