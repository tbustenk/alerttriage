"""Report exporters: PDF, CSV, and enhanced HTML.

PDF uses fpdf2 (pure Python, no system libraries required).
CSV uses stdlib csv — no extra dependencies.
HTML extends the existing report_generator template with analytics sections.
"""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any

from alerttriage.src.analytics.engine import ClientReport
from alerttriage.src.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# HTML exporter
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>AlertTriage Analytics Report – {client_id}</title>
<style>
  :root {{
    --blue: #2563eb; --green: #16a34a; --red: #dc2626;
    --amber: #d97706; --gray: #64748b; --bg: #f8fafc;
  }}
  body {{ font-family: system-ui, sans-serif; margin: 0; background: var(--bg); color: #1e293b; }}
  .header {{ background: var(--blue); color: white; padding: 2rem; }}
  .header h1 {{ margin: 0; font-size: 1.6rem; }}
  .header p  {{ margin: .3rem 0 0; opacity: .85; font-size: .9rem; }}
  .container {{ max-width: 960px; margin: 2rem auto; padding: 0 1rem; }}
  .exec {{ background: #eff6ff; border-left: 4px solid var(--blue);
           padding: 1rem 1.5rem; border-radius: 4px; margin-bottom: 2rem;
           font-size: 1.05rem; line-height: 1.6; }}
  .kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
               gap: 1rem; margin-bottom: 2rem; }}
  .kpi {{ background: white; border-radius: 8px; padding: 1.2rem;
          box-shadow: 0 1px 3px rgba(0,0,0,.1); text-align: center; }}
  .kpi .val {{ font-size: 1.8rem; font-weight: 700; color: var(--blue); }}
  .kpi .label {{ font-size: .8rem; color: var(--gray); margin-top: .3rem; }}
  .card {{ background: white; border-radius: 8px; padding: 1.5rem;
           box-shadow: 0 1px 3px rgba(0,0,0,.1); margin-bottom: 1.5rem; }}
  .card h2 {{ margin-top: 0; font-size: 1.1rem; color: #0f172a;
              border-bottom: 1px solid #e2e8f0; padding-bottom: .5rem; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .9rem; }}
  th, td {{ text-align: left; padding: .5rem .75rem; border-bottom: 1px solid #e2e8f0; }}
  th {{ background: #f1f5f9; font-weight: 600; }}
  tr:last-child td {{ border-bottom: none; }}
  .badge {{ display: inline-block; padding: .15rem .6rem; border-radius: 9999px; font-size: .75rem; font-weight: 600; }}
  .good  {{ background: #dcfce7; color: var(--green); }}
  .warn  {{ background: #fef3c7; color: var(--amber); }}
  .bad   {{ background: #fee2e2; color: var(--red); }}
  .trend-up   {{ color: var(--green); }}
  .trend-down {{ color: var(--red); }}
  .recs li {{ margin: .4rem 0; }}
  .footer {{ text-align: center; color: var(--gray); font-size: .8rem; margin: 2rem 0; }}
  .roi-highlight {{ background: linear-gradient(135deg, #eff6ff, #dbeafe);
                    border-radius: 8px; padding: 1.5rem; margin-bottom: 1.5rem; }}
  .roi-highlight .net {{ font-size: 2.2rem; font-weight: 800; color: var(--blue); }}
</style>
</head>
<body>

<div class="header">
  <h1>AlertTriage Analytics Report</h1>
  <p>Client: <strong>{client_id}</strong> &nbsp;|&nbsp; Period: Last {period_days} days &nbsp;|&nbsp;
     Generated: {generated_at}</p>
</div>

<div class="container">

  <!-- Executive summary -->
  <div class="exec">{executive_summary}</div>

  <!-- KPI tiles -->
  <div class="kpi-grid">
    <div class="kpi"><div class="val">{total_alerts:,}</div><div class="label">Alerts Analyzed</div></div>
    <div class="kpi"><div class="val">{accuracy_pct}</div><div class="label">AI Accuracy</div></div>
    <div class="kpi"><div class="val">{hours_saved:.0f}h</div><div class="label">Analyst Hours Saved</div></div>
    <div class="kpi"><div class="val">${labor_value:,.0f}</div><div class="label">Labor Value Saved</div></div>
    <div class="kpi"><div class="val">${ai_cost:.2f}</div><div class="label">AI Cost</div></div>
    <div class="kpi"><div class="val">{roi_pct:.0f}%</div><div class="label">ROI</div></div>
  </div>

  <!-- ROI card -->
  <div class="roi-highlight">
    <h2 style="margin-top:0">Return on Investment</h2>
    <div class="net">${net_roi:,.0f} net value</div>
    <p style="margin:.5rem 0 0; color:#334155">
      {alerts_analyzed:,} alerts × {analyst_min:.0f} min saved each @
      ${hourly_rate:.0f}/hr = <strong>${labor_value:,.0f}</strong> labor value &minus;
      <strong>${ai_cost:.2f}</strong> AI cost =
      <strong style="color:{roi_color}">${net_roi:,.0f}</strong> &nbsp;
      ({payback:.1f}× payback ratio)
    </p>
    <p style="margin:.3rem 0 0; font-size:.85rem; color:#64748b">
      Monthly projection: ${monthly_projection:,.0f}
    </p>
  </div>

  <!-- Weekly breakdown -->
  <div class="card">
    <h2>Weekly Breakdown</h2>
    <table>
      <tr><th>Week</th><th>Alerts</th><th>Correct</th><th>False Positives</th><th>Accuracy</th></tr>
      {weekly_rows}
    </table>
  </div>

  <!-- Alert type breakdown -->
  <div class="card">
    <h2>Alert Type Analysis</h2>
    <table>
      <tr><th>Rule</th><th>Total</th><th>FP Rate</th><th>Accuracy</th><th>Trend</th></tr>
      {rule_rows}
    </table>
  </div>

  <!-- FP analysis -->
  <div class="card">
    <h2>False Positive Analysis <small style="color:#64748b;font-weight:400">({total_fps} FPs, {fp_rate_pct} overall)</small></h2>
    {fp_patterns_html}
    <h3 style="margin-bottom:.5rem">Recommendations</h3>
    <ul class="recs">{recs_html}</ul>
  </div>

  <!-- Accuracy trend -->
  <div class="card">
    <h2>Accuracy Trend
      <span class="badge {trend_badge}">{trend_dir}</span>
      <small style="color:#64748b;font-weight:400;font-size:.8rem"> ({trend_change:+.1f}% over period)</small>
    </h2>
    <table>
      <tr><th>Week</th><th>Accuracy</th><th>FP Rate</th><th>Volume</th></tr>
      {trend_rows}
    </table>
  </div>

</div>

<div class="footer">Generated by AlertTriage v2 on {generated_at} UTC</div>
</body>
</html>
"""


class HTMLExporter:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def export(self, report: ClientReport) -> Path:
        ws  = report.weekly_summary
        roi = report.roi_report
        fp  = report.fp_analysis
        bd  = report.alert_type_breakdown
        tr  = report.trend_report

        weekly_rows = "\n".join(
            f"<tr><td>{w.week}</td><td>{w.total:,}</td><td>{w.correct:,}</td>"
            f"<td>{w.false_positives:,}</td>"
            f"<td><span class='badge {'good' if w.accuracy>=.8 else 'warn' if w.accuracy>=.6 else 'bad'}'>"
            f"{w.accuracy:.0%}</span></td></tr>"
            for w in ws.weeks
        )

        def _trend_badge(t: str) -> str:
            return {"improving": "trend-up", "worsening": "trend-down"}.get(t, "")

        rule_rows = "\n".join(
            f"<tr><td>{r.rule_name}</td><td>{r.total:,}</td>"
            f"<td><span class='badge {'bad' if r.fp_rate>.4 else 'warn' if r.fp_rate>.2 else 'good'}'>"
            f"{r.fp_rate:.0%}</span></td>"
            f"<td>{r.accuracy:.0%}</td>"
            f"<td class='{_trend_badge(r.trend)}'>{r.trend.replace('_', ' ')}</td></tr>"
            for r in bd.rules[:15]
        )

        fp_patterns_html = ""
        if fp.patterns:
            fp_patterns_html = (
                "<table><tr><th>Pattern</th><th>Count</th><th>Rules</th></tr>"
                + "\n".join(
                    f"<tr><td>{p.description}</td><td>{p.count}</td><td>{', '.join(p.rules[:3])}</td></tr>"
                    for p in fp.patterns
                )
                + "</table>"
            )

        recs_html = "\n".join(f"<li>{r}</li>" for r in fp.recommendations)

        pairs = list(zip(tr.accuracy_trend, tr.fp_rate_trend))
        trend_rows = "\n".join(
            f"<tr><td>{a.period}</td><td>{a.value:.0%}</td><td>{f.value:.0%}</td><td>{a.count:,}</td></tr>"
            for a, f in pairs[-8:]
        )

        trend_badge = {"improving": "good", "declining": "bad"}.get(tr.accuracy_direction, "warn")

        html = _HTML_TEMPLATE.format(
            client_id=report.client_id,
            period_days=report.period_days,
            generated_at=report.generated_at.strftime("%Y-%m-%d %H:%M"),
            executive_summary=report.executive_summary,
            total_alerts=ws.total_alerts,
            accuracy_pct=f"{ws.overall_accuracy:.0%}",
            hours_saved=ws.estimated_hours_saved,
            labor_value=ws.labor_value_usd,
            ai_cost=ws.ai_cost_usd,
            roi_pct=roi.roi_percentage,
            net_roi=roi.net_roi_usd,
            alerts_analyzed=roi.alerts_analyzed,
            analyst_min=roi.analyst_minutes_per_alert,
            hourly_rate=roi.analyst_hourly_rate,
            payback=roi.payback_ratio,
            monthly_projection=roi.monthly_projection_usd,
            roi_color="#16a34a" if roi.net_roi_usd >= 0 else "#dc2626",
            weekly_rows=weekly_rows or "<tr><td colspan='5' style='color:#94a3b8'>No data yet</td></tr>",
            rule_rows=rule_rows or "<tr><td colspan='5' style='color:#94a3b8'>No data yet</td></tr>",
            total_fps=fp.total_false_positives,
            fp_rate_pct=f"{fp.fp_rate:.0%}",
            fp_patterns_html=fp_patterns_html,
            recs_html=recs_html,
            trend_rows=trend_rows or "<tr><td colspan='4' style='color:#94a3b8'>No trend data yet</td></tr>",
            trend_dir=tr.accuracy_direction,
            trend_badge=trend_badge,
            trend_change=tr.accuracy_change_pct,
        )

        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        path = self.output_dir / f"analytics_{report.client_id}_{ts}.html"
        path.write_text(html, encoding="utf-8")
        log.info("html_report_exported", client=report.client_id, path=str(path))
        return path


# ---------------------------------------------------------------------------
# CSV exporter
# ---------------------------------------------------------------------------


class CSVExporter:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def export(self, report: ClientReport) -> list[Path]:
        """Export one CSV per section. Returns list of paths created."""
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        cid = report.client_id
        paths: list[Path] = []

        # Weekly summary
        paths.append(self._write(
            f"weekly_{cid}_{ts}.csv",
            ["week", "total", "correct", "false_positives", "accuracy"],
            [
                [w.week, w.total, w.correct, w.false_positives, f"{w.accuracy:.4f}"]
                for w in report.weekly_summary.weeks
            ],
        ))

        # Alert type breakdown
        paths.append(self._write(
            f"rules_{cid}_{ts}.csv",
            ["rule_name", "total", "correct", "false_positives", "fp_rate", "accuracy", "trend"],
            [
                [r.rule_name, r.total, r.correct, r.false_positives,
                 f"{r.fp_rate:.4f}", f"{r.accuracy:.4f}", r.trend]
                for r in report.alert_type_breakdown.rules
            ],
        ))

        # ROI
        roi = report.roi_report
        paths.append(self._write(
            f"roi_{cid}_{ts}.csv",
            ["metric", "value"],
            [
                ["client_id", cid],
                ["period_days", roi.period_days],
                ["alerts_analyzed", roi.alerts_analyzed],
                ["analyst_minutes_per_alert", roi.analyst_minutes_per_alert],
                ["analyst_hourly_rate", roi.analyst_hourly_rate],
                ["hours_saved", roi.hours_saved],
                ["labor_value_usd", roi.labor_value_usd],
                ["ai_cost_usd", roi.ai_cost_usd],
                ["net_roi_usd", roi.net_roi_usd],
                ["roi_percentage", roi.roi_percentage],
                ["payback_ratio", roi.payback_ratio],
                ["monthly_projection_usd", roi.monthly_projection_usd],
            ],
        ))

        # Accuracy trend
        paths.append(self._write(
            f"trends_{cid}_{ts}.csv",
            ["week", "accuracy", "fp_rate", "volume"],
            [
                [a.period, f"{a.value:.4f}", f"{f.value:.4f}", a.count]
                for a, f in zip(report.trend_report.accuracy_trend, report.trend_report.fp_rate_trend)
            ],
        ))

        log.info("csv_report_exported", client=cid, files=len(paths))
        return paths

    def _write(self, filename: str, headers: list[str], rows: list[list]) -> Path:
        path = self.output_dir / filename
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(headers)
            w.writerows(rows)
        return path


# ---------------------------------------------------------------------------
# PDF exporter (fpdf2)
# ---------------------------------------------------------------------------


class PDFExporter:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def export(self, report: ClientReport) -> Path:
        try:
            from fpdf import FPDF
        except ImportError as exc:
            raise ImportError(
                "fpdf2 is required for PDF export. "
                "Install with: pip install fpdf2"
            ) from exc

        pdf = _AlertTriagePDF(report)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        path = self.output_dir / f"report_{report.client_id}_{ts}.pdf"
        pdf.output(str(path))
        log.info("pdf_report_exported", client=report.client_id, path=str(path))
        return path


class _AlertTriagePDF:
    """Build a multi-page PDF from a ClientReport using fpdf2."""

    BLUE  = (37,  99, 235)
    GREEN = (22, 163, 74)
    RED   = (220, 38, 38)
    GRAY  = (100, 116, 139)
    WHITE = (255, 255, 255)
    DARK  = (15, 23, 42)
    LIGHT = (248, 250, 252)

    def __init__(self, report: ClientReport) -> None:
        from fpdf import FPDF

        self._r = report
        self.pdf = FPDF(orientation="P", unit="mm", format="A4")
        self.pdf.set_auto_page_break(auto=True, margin=15)
        self.pdf.set_margins(15, 15, 15)
        self._build()

    def output(self, path: str) -> None:
        self.pdf.output(path)

    def _build(self) -> None:
        self._cover_page()
        self._roi_section()
        self._weekly_section()
        self._rule_breakdown_section()
        self._fp_section()
        self._trend_section()

    def _cover_page(self) -> None:
        pdf = self.pdf
        pdf.add_page()

        pdf.set_fill_color(*self.BLUE)
        pdf.rect(0, 0, 210, 55, "F")
        pdf.set_text_color(*self.WHITE)
        pdf.set_font("Helvetica", "B", 22)
        pdf.set_xy(15, 15)
        pdf.cell(180, 10, "AlertTriage Analytics Report", ln=True)
        pdf.set_font("Helvetica", "", 11)
        pdf.set_xy(15, 30)
        pdf.cell(180, 8, f"Client: {self._r.client_id}", ln=True)
        pdf.set_xy(15, 40)
        pdf.cell(180, 8, f"Period: Last {self._r.period_days} days  |  Generated: {self._r.generated_at.strftime('%Y-%m-%d %H:%M UTC')}", ln=True)

        pdf.set_text_color(*self.DARK)
        pdf.set_xy(15, 65)
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(180, 8, "Executive Summary", ln=True)
        pdf.set_font("Helvetica", "", 10)
        pdf.set_xy(15, 75)
        pdf.multi_cell(180, 6, self._r.executive_summary)

        # KPI boxes
        ws  = self._r.weekly_summary
        roi = self._r.roi_report
        kpis = [
            ("Alerts Analyzed",   f"{ws.total_alerts:,}"),
            ("AI Accuracy",       f"{ws.overall_accuracy:.0%}"),
            ("Hours Saved",       f"{ws.estimated_hours_saved:.0f}h"),
            ("Labor Value",       f"${ws.labor_value_usd:,.0f}"),
            ("Net ROI",           f"${roi.net_roi_usd:,.0f}"),
            ("ROI %",             f"{roi.roi_percentage:.0f}%"),
        ]
        x0 = 15
        y0 = pdf.get_y() + 10
        box_w, box_h, gap = 55, 22, 5
        for i, (label, val) in enumerate(kpis):
            col = i % 3
            row = i // 3
            x = x0 + col * (box_w + gap)
            y = y0 + row * (box_h + gap)
            pdf.set_fill_color(239, 246, 255)
            pdf.rect(x, y, box_w, box_h, "F")
            pdf.set_font("Helvetica", "B", 14)
            pdf.set_text_color(*self.BLUE)
            pdf.set_xy(x, y + 4)
            pdf.cell(box_w, 8, val, align="C", ln=False)
            pdf.set_font("Helvetica", "", 8)
            pdf.set_text_color(*self.GRAY)
            pdf.set_xy(x, y + 13)
            pdf.cell(box_w, 6, label, align="C")

    def _roi_section(self) -> None:
        roi = self._r.roi_report
        self.pdf.add_page()
        self._section_title("Return on Investment")
        rows = [
            ["Alerts analyzed", f"{roi.alerts_analyzed:,}"],
            ["Analyst minutes saved per alert", f"{roi.analyst_minutes_per_alert:.0f} min"],
            ["Analyst hourly rate", f"${roi.analyst_hourly_rate:.0f}"],
            ["Total hours saved", f"{roi.hours_saved:.1f} h"],
            ["Labor value saved", f"${roi.labor_value_usd:,.2f}"],
            ["AI API cost", f"${roi.ai_cost_usd:.4f}"],
            ["Net ROI", f"${roi.net_roi_usd:,.2f}"],
            ["ROI %", f"{roi.roi_percentage:.1f}%"],
            ["Payback ratio", f"{roi.payback_ratio:.1f}×"],
            ["Monthly projection", f"${roi.monthly_projection_usd:,.2f}"],
        ]
        self._table(["Metric", "Value"], rows)

    def _weekly_section(self) -> None:
        ws = self._r.weekly_summary
        self._section_title("Weekly Summary")
        rows = [
            [w.week, str(w.total), str(w.correct), str(w.false_positives), f"{w.accuracy:.0%}"]
            for w in ws.weeks
        ]
        self._table(["Week", "Total", "Correct", "FP", "Accuracy"], rows)

    def _rule_breakdown_section(self) -> None:
        bd = self._r.alert_type_breakdown
        self._section_title("Alert Type Breakdown (top 15)")
        rows = [
            [r.rule_name[:28], str(r.total), f"{r.fp_rate:.0%}", f"{r.accuracy:.0%}", r.trend]
            for r in bd.rules[:15]
        ]
        self._table(["Rule", "Total", "FP Rate", "Accuracy", "Trend"], rows)

    def _fp_section(self) -> None:
        fp = self._r.fp_analysis
        self._section_title("False Positive Analysis")
        self.pdf.set_font("Helvetica", "", 10)
        self.pdf.set_text_color(*self.DARK)
        self.pdf.cell(0, 6, f"Total FPs: {fp.total_false_positives}  |  FP Rate: {fp.fp_rate:.0%}", ln=True)
        self.pdf.ln(3)

        if fp.patterns:
            self._section_title("Common FP Patterns", size=11)
            rows = [[p.description, str(p.count), ", ".join(p.rules[:3])] for p in fp.patterns]
            self._table(["Pattern", "Count", "Rules"], rows)

        self._section_title("Recommendations", size=11)
        pdf = self.pdf
        for rec in fp.recommendations:
            pdf.set_font("Helvetica", "", 10)
            pdf.set_text_color(*self.DARK)
            pdf.set_x(15)
            pdf.multi_cell(180, 6, f"• {rec}")
            pdf.ln(1)

    def _trend_section(self) -> None:
        tr = self._r.trend_report
        self._section_title("Accuracy Trend")
        pairs = list(zip(tr.accuracy_trend, tr.fp_rate_trend))[-12:]
        rows = [[a.period, f"{a.value:.0%}", f"{f.value:.0%}", str(a.count)] for a, f in pairs]
        self._table(["Week", "Accuracy", "FP Rate", "Volume"], rows)

        pdf = self.pdf
        pdf.ln(4)
        pdf.set_font("Helvetica", "B", 10)
        colour = self.GREEN if tr.accuracy_direction == "improving" else (self.RED if tr.accuracy_direction == "declining" else self.GRAY)
        pdf.set_text_color(*colour)
        pdf.cell(0, 6, f"Trend: {tr.accuracy_direction}  ({tr.accuracy_change_pct:+.1f}% over period)", ln=True)

    def _section_title(self, text: str, size: int = 13) -> None:
        pdf = self.pdf
        pdf.ln(4)
        pdf.set_font("Helvetica", "B", size)
        pdf.set_text_color(*self.BLUE)
        pdf.cell(0, 8, text, ln=True)
        pdf.set_draw_color(*self.BLUE)
        pdf.line(15, pdf.get_y(), 195, pdf.get_y())
        pdf.ln(3)
        pdf.set_text_color(*self.DARK)

    def _table(self, headers: list[str], rows: list[list[str]], col_w: float | None = None) -> None:
        pdf = self.pdf
        n = len(headers)
        w = col_w or (180 / n)
        pdf.set_fill_color(241, 245, 249)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(*self.DARK)
        for h in headers:
            pdf.cell(w, 7, str(h)[:22], border=1, fill=True)
        pdf.ln()
        pdf.set_font("Helvetica", "", 9)
        for i, row in enumerate(rows):
            fill = i % 2 == 0
            pdf.set_fill_color(248, 250, 252 if fill else 255, )
            for cell in row:
                pdf.cell(w, 6, str(cell)[:22], border=1, fill=fill)
            pdf.ln()
        pdf.ln(3)


# ---------------------------------------------------------------------------
# JSON exporter (thin wrapper — uses engine.to_dict)
# ---------------------------------------------------------------------------


class JSONExporter:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def export(self, report: ClientReport, engine: Any) -> Path:
        data = engine.to_dict(report)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        path = self.output_dir / f"analytics_{report.client_id}_{ts}.json"
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        log.info("json_report_exported", client=report.client_id, path=str(path))
        return path
