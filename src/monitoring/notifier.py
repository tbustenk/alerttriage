"""Email and Slack notification sender for AlertTriage health alerts.

Configured via environment variables or the ``notification_config`` dict
passed to :class:`~alerttriage.src.monitoring.health_monitor.HealthMonitor`.

Notifications are rate-limited by ``cooldown_sec`` (default 300 s) so a
flapping threshold doesn't produce a flood of messages.
"""

from __future__ import annotations

import asyncio
import smtplib
import time
from email.mime.text import MIMEText
from typing import Any

import httpx

from alerttriage.src.logger import get_logger

log = get_logger(__name__)


class Notifier:
    """Send alerts via email and/or Slack webhook.

    Args:
        config: Dict with optional ``email`` and ``slack_webhook_url`` keys.
            ``email`` sub-dict: ``to_addr``, ``from_addr``, ``smtp_host``,
            ``smtp_port``, ``use_tls``, ``username``, ``password``.
        cooldown_sec: Minimum seconds between notifications (per channel).
    """

    def __init__(
        self,
        config: dict[str, Any],
        *,
        cooldown_sec: int = 300,
    ) -> None:
        self._email_cfg: dict[str, Any] | None = config.get("email")
        self._slack_url: str | None = config.get("slack_webhook_url")
        self._cooldown = cooldown_sec
        self._last_sent: float = 0.0

    async def send_alert(self, *, subject: str, body: str) -> None:
        """Broadcast ``subject`` + ``body`` to all configured channels.

        Silently drops the notification if within the cooldown window.
        Errors in individual channels are logged but do not propagate.
        """
        now = time.monotonic()
        if now - self._last_sent < self._cooldown:
            log.debug("notification_suppressed_cooldown", cooldown_sec=self._cooldown)
            return
        self._last_sent = now

        tasks: list[Any] = []
        if self._email_cfg:
            tasks.append(self._send_email(subject=subject, body=body))
        if self._slack_url:
            tasks.append(self._send_slack(subject=subject, body=body))

        if not tasks:
            return

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                log.error("notification_channel_failed", error=str(result))

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _send_email(self, *, subject: str, body: str) -> None:
        cfg = self._email_cfg
        if not cfg:
            return
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = cfg.get("from_addr", "alerttriage@localhost")
        msg["To"] = cfg["to_addr"]

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._smtp_send, cfg, msg)
        log.info("email_alert_sent", to=cfg["to_addr"], subject=subject)

    def _smtp_send(self, cfg: dict[str, Any], msg: MIMEText) -> None:
        host = cfg.get("smtp_host", "localhost")
        port = int(cfg.get("smtp_port", 587))
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            if cfg.get("use_tls", True):
                smtp.starttls()
            if cfg.get("username"):
                smtp.login(cfg["username"], cfg.get("password", ""))
            smtp.send_message(msg)

    async def _send_slack(self, *, subject: str, body: str) -> None:
        payload = {"text": f"*{subject}*\n```{body}```"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(self._slack_url, json=payload)  # type: ignore[arg-type]
            resp.raise_for_status()
        log.info("slack_alert_sent", subject=subject)
