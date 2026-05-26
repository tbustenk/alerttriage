"""Scheduled report generation and email delivery.

Uses a lightweight asyncio-based scheduler (no external dependency) that
checks every minute whether any client has a report due.

Schedule config lives in ``data/<client_id>/report_schedule.json``:

  {
    "frequency": "weekly",          # daily | weekly | monthly
    "day_of_week": 1,               # 0=Mon…6=Sun  (weekly only)
    "hour": 9,                      # UTC hour to send (0-23)
    "email": "soc@client.com",
    "format": "html",               # html | pdf | json | csv
    "analyst_hourly_rate": 75.0,
    "analyst_minutes_per_alert": 15.0,
    "enabled": true
  }
"""

from __future__ import annotations

import asyncio
import json
import smtplib
from datetime import datetime, timezone
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from pathlib import Path
from typing import Any, TYPE_CHECKING

from alerttriage.src.logger import get_logger

if TYPE_CHECKING:
    from alerttriage.config.config_manager import ConfigManager

log = get_logger(__name__)

_SCHEDULE_FILENAME = "report_schedule.json"


class ReportScheduler:
    """Runs as an asyncio background task; fires scheduled reports.

    Args:
        data_dir:    Root data directory.
        config:      ConfigManager (for client list and SMTP config).
        reports_dir: Directory to save generated report files.
        smtp_config: SMTP connection settings (from env or config).
        check_interval_sec: How often to poll for due reports (default 60 s).
    """

    def __init__(
        self,
        data_dir: Path,
        config: ConfigManager,
        reports_dir: Path,
        smtp_config: dict[str, Any] | None = None,
        check_interval_sec: int = 60,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._config = config
        self._reports_dir = Path(reports_dir)
        self._smtp = smtp_config or {}
        self._interval = check_interval_sec
        self._running = False

    # ------------------------------------------------------------------
    # Schedule management
    # ------------------------------------------------------------------

    def get_schedule(self, client_id: str) -> dict[str, Any] | None:
        path = self._schedule_path(client_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def set_schedule(self, client_id: str, schedule: dict[str, Any]) -> None:
        path = self._schedule_path(client_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(schedule, indent=2), encoding="utf-8")
        log.info("report_schedule_saved", client=client_id, frequency=schedule.get("frequency"))

    def delete_schedule(self, client_id: str) -> bool:
        path = self._schedule_path(client_id)
        if path.exists():
            path.unlink()
            return True
        return False

    # ------------------------------------------------------------------
    # Async runner
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start the scheduler loop. Runs until cancelled."""
        self._running = True
        log.info("report_scheduler_started", interval_sec=self._interval)
        while self._running:
            try:
                await self._check_all()
            except Exception as exc:  # noqa: BLE001
                log.error("scheduler_tick_failed", error=str(exc))
            await asyncio.sleep(self._interval)

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _check_all(self) -> None:
        now = datetime.now(timezone.utc)
        for client_id in self._config.list_clients():
            schedule = self.get_schedule(client_id)
            if not schedule or not schedule.get("enabled", True):
                continue
            if self._is_due(schedule, now):
                await self._fire_report(client_id, schedule)

    def _is_due(self, schedule: dict[str, Any], now: datetime) -> bool:
        freq = schedule.get("frequency", "weekly")
        hour = int(schedule.get("hour", 9))
        if now.hour != hour or now.minute != 0:
            return False
        if freq == "daily":
            return True
        if freq == "weekly":
            dow = int(schedule.get("day_of_week", 1))  # 0=Mon
            return now.weekday() == dow
        if freq == "monthly":
            return now.day == 1
        return False

    async def _fire_report(self, client_id: str, schedule: dict[str, Any]) -> None:
        log.info("scheduled_report_firing", client=client_id, format=schedule.get("format", "html"))
        try:
            from alerttriage.src.analytics.engine import AnalyticsEngine
            from alerttriage.src.analytics.exporters import CSVExporter, HTMLExporter, JSONExporter, PDFExporter

            engine = AnalyticsEngine(self._data_dir, self._config)
            report = engine.generate_client_report(
                client_id,
                period_days=30,
                analyst_hourly_rate=float(schedule.get("analyst_hourly_rate", 75.0)),
                analyst_minutes_per_alert=float(schedule.get("analyst_minutes_per_alert", 15.0)),
            )

            fmt = schedule.get("format", "html")
            out_dir = self._reports_dir / client_id
            if fmt == "pdf":
                path = PDFExporter(out_dir).export(report)
            elif fmt == "csv":
                paths = CSVExporter(out_dir).export(report)
                path = paths[0]  # attach first file; email body has summary
            elif fmt == "json":
                path = JSONExporter(out_dir).export(report, engine)
            else:
                path = HTMLExporter(out_dir).export(report)

            email_to = schedule.get("email")
            if email_to and self._smtp:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    self._send_email,
                    email_to,
                    client_id,
                    report.executive_summary,
                    path,
                    fmt,
                )
                log.info("scheduled_report_emailed", client=client_id, to=email_to)
        except Exception as exc:  # noqa: BLE001
            log.error("scheduled_report_failed", client=client_id, error=str(exc))

    def _send_email(
        self,
        to: str,
        client_id: str,
        summary: str,
        report_path: Path,
        fmt: str,
    ) -> None:
        subject = f"AlertTriage Weekly Report — {client_id}"
        msg = MIMEMultipart()
        msg["Subject"] = subject
        msg["From"] = self._smtp.get("from_addr", "alerttriage@localhost")
        msg["To"] = to

        body = MIMEText(
            f"Hi,\n\nYour AlertTriage report for {client_id} is attached.\n\n"
            f"{summary}\n\n-- AlertTriage v2",
            "plain",
            "utf-8",
        )
        msg.attach(body)

        if report_path.exists():
            with open(report_path, "rb") as fh:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(fh.read())
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f"attachment; filename={report_path.name}",
            )
            msg.attach(part)

        host = self._smtp.get("smtp_host", "localhost")
        port = int(self._smtp.get("smtp_port", 587))
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            if self._smtp.get("use_tls", True):
                smtp.starttls()
            if self._smtp.get("username"):
                smtp.login(self._smtp["username"], self._smtp.get("password", ""))
            smtp.send_message(msg)

    def _schedule_path(self, client_id: str) -> Path:
        return self._data_dir / client_id / _SCHEDULE_FILENAME
