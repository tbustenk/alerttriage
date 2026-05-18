"""Splunk Enterprise Security connector (REST API v2)."""

from __future__ import annotations

import httpx
from typing import Any

from alerttriage.src.core.alert_models import Alert, AlertSeverity, AlertSource, AlertContext
from alerttriage.src.core.result_models import AnalysisResult
from alerttriage.src.integrations.siem_base import SIEMConnector
from alerttriage.src.logger import get_logger

log = get_logger(__name__)

_SEVERITY_MAP = {
    "critical": AlertSeverity.CRITICAL,
    "high": AlertSeverity.HIGH,
    "medium": AlertSeverity.MEDIUM,
    "low": AlertSeverity.LOW,
    "informational": AlertSeverity.INFO,
}


class SplunkConnector(SIEMConnector):
    """
    Reads notable events from Splunk ES and writes verdicts back as comments.

    Required config keys:
      host, port, token (or username+password), verify_ssl, client_id
    Optional:
      earliest_time (-15m default), index, es_owner, es_status_filter
    """

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._base = (
            f"https://{config['host']}:{config.get('port', 8089)}/services"
        )
        self._token = config.get("token")
        self._verify = config.get("verify_ssl", True)
        self._client_id = config["client_id"]
        self._session: httpx.AsyncClient | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _get_session(self) -> httpx.AsyncClient:
        if self._session is None or self._session.is_closed:
            self._session = httpx.AsyncClient(
                headers=self._headers(),
                verify=self._verify,
                timeout=30.0,
            )
        return self._session

    async def health_check(self) -> bool:
        session = await self._get_session()
        try:
            r = await session.get(f"{self._base}/server/info?output_mode=json")
            return r.status_code == 200
        except Exception as exc:
            log.warning("splunk_health_check_failed", error=str(exc))
            return False

    async def fetch_alerts(
        self,
        *,
        limit: int = 100,
        since_id: str | None = None,
    ) -> list[Alert]:
        session = await self._get_session()
        earliest = self.config.get("earliest_time", "-15m")
        params = {
            "output_mode": "json",
            "count": limit,
            "earliest_time": earliest,
            "search": self.config.get(
                "search_query",
                "| inputlookup notable | where status=0",
            ),
        }
        try:
            r = await session.get(
                f"{self._base}/search/jobs/export", params=params
            )
            r.raise_for_status()
            return [self._parse_notable(row) for row in r.json().get("results", [])]
        except Exception as exc:
            log.error("splunk_fetch_failed", error=str(exc))
            return []

    def _parse_notable(self, row: dict[str, Any]) -> Alert:
        sev_str = str(row.get("urgency", "medium")).lower()
        return Alert(
            client_id=self._client_id,
            source=AlertSource.SPLUNK,
            rule_name=row.get("rule_name", "unknown"),
            severity=_SEVERITY_MAP.get(sev_str, AlertSeverity.MEDIUM),
            title=row.get("rule_title", row.get("rule_name", "Splunk Notable")),
            description=row.get("rule_description", ""),
            source_alert_id=row.get("event_id"),
            raw_payload=row,
            context=AlertContext(
                host_info={"hostname": row.get("dest", ""), "ip": row.get("dest_ip", "")},
                user_info={"username": row.get("user", "")},
            ),
            tags=str(row.get("tags", "")).split(","),
        )

    async def send_result(self, result: AnalysisResult) -> bool:
        if not result.metadata.get("source_alert_id"):
            return False
        session = await self._get_session()
        payload = {
            "comment": (
                f"[AlertTriage AI] Verdict: {result.verdict.value} "
                f"(confidence: {result.confidence:.0%})\n\n"
                f"{result.summary}\n\n"
                f"Reasoning: {result.reasoning[:500]}"
            ),
            "status": "1" if result.verdict.value == "true_positive" else "4",
        }
        event_id = result.metadata["source_alert_id"]
        try:
            r = await session.post(
                f"{self._base}/notable_update",
                json={"ruleUIDs": [event_id], **payload},
            )
            return r.status_code in (200, 201)
        except Exception as exc:
            log.error("splunk_send_result_failed", error=str(exc))
            return False
