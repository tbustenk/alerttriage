"""Splunk Enterprise Security connector (REST API v2).

Reads notable events and writes verdicts back as comments on the notable.
The connector holds a long-lived :class:`httpx.AsyncClient` so token
authentication and TLS handshakes are reused across requests.
"""

from __future__ import annotations

from typing import Any

import httpx

from alerttriage.src.core.alert_models import Alert, AlertContext, AlertSeverity, AlertSource
from alerttriage.src.core.result_models import AnalysisResult
from alerttriage.src.integrations.siem_base import SIEMConnector
from alerttriage.src.logger import get_logger

log = get_logger(__name__)

_SEVERITY_MAP: dict[str, AlertSeverity] = {
    "critical": AlertSeverity.CRITICAL,
    "high": AlertSeverity.HIGH,
    "medium": AlertSeverity.MEDIUM,
    "low": AlertSeverity.LOW,
    "informational": AlertSeverity.INFO,
    "info": AlertSeverity.INFO,
}


class SplunkConnector(SIEMConnector):
    """Reads notable events from Splunk ES and writes verdicts back as comments.

    Required config keys:
      ``host``, ``client_id``, and either ``token`` or
      ``ALERTTRIAGE_<CLIENT_ID_UPPER>_API_KEY`` set in the environment.
    Optional config keys:
      ``port`` (default 8089), ``verify_ssl`` (default True),
      ``earliest_time`` (default "-15m"), ``search_query``, ``timeout_sec``.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        if "host" not in config:
            raise ValueError("SplunkConnector requires a 'host' in its config")
        if "client_id" not in config:
            raise ValueError("SplunkConnector requires a 'client_id' in its config")
        self._base = f"https://{config['host']}:{config.get('port', 8089)}/services"
        self._token = config.get("token") or config.get("api_key")
        self._verify = bool(config.get("verify_ssl", True))
        self._timeout = float(config.get("timeout_sec", 30.0))
        self._client_id = str(config["client_id"])
        self._session: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        if not self._token:
            log.warning("splunk_no_token", client=self._client_id)
        return {
            "Authorization": f"Bearer {self._token or ''}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _get_session(self) -> httpx.AsyncClient:
        if self._session is None or self._session.is_closed:
            self._session = httpx.AsyncClient(
                headers=self._headers(),
                verify=self._verify,
                timeout=self._timeout,
            )
        return self._session

    async def close(self) -> None:
        """Release the underlying HTTP session (call from shutdown handlers)."""
        if self._session is not None and not self._session.is_closed:
            await self._session.aclose()
            self._session = None

    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        session = await self._get_session()
        try:
            r = await session.get(f"{self._base}/server/info?output_mode=json")
            return r.status_code == 200
        except httpx.HTTPError as exc:
            log.warning("splunk_health_check_failed", error=str(exc))
            return False

    async def fetch_alerts(
        self,
        *,
        limit: int = 100,
        since_id: str | None = None,
    ) -> list[Alert]:
        session = await self._get_session()
        params = {
            "output_mode": "json",
            "count": limit,
            "earliest_time": self.config.get("earliest_time", "-15m"),
            "search": self.config.get(
                "search_query",
                "| inputlookup notable | where status=0",
            ),
        }
        try:
            r = await session.get(f"{self._base}/search/jobs/export", params=params)
            r.raise_for_status()
            payload = r.json() or {}
            results = payload.get("results") or []
        except httpx.HTTPError as exc:
            log.error("splunk_fetch_failed", error=str(exc))
            return []
        except ValueError as exc:  # malformed JSON
            log.error("splunk_fetch_invalid_json", error=str(exc))
            return []

        alerts: list[Alert] = []
        for row in results:
            try:
                alerts.append(self._parse_notable(row))
            except Exception as exc:  # noqa: BLE001 — never poison a batch
                log.warning("splunk_parse_failed", error=str(exc))
        return alerts

    def _parse_notable(self, row: dict[str, Any]) -> Alert:
        sev_str = str(row.get("urgency", "medium")).lower()
        return Alert(
            client_id=self._client_id,
            source=AlertSource.SPLUNK,
            rule_name=str(row.get("rule_name", "unknown")),
            severity=_SEVERITY_MAP.get(sev_str, AlertSeverity.MEDIUM),
            title=str(row.get("rule_title", row.get("rule_name", "Splunk Notable"))),
            description=str(row.get("rule_description", "")),
            source_alert_id=row.get("event_id"),
            raw_payload=row,
            context=AlertContext(
                host_info={"hostname": row.get("dest", ""), "ip": row.get("dest_ip", "")},
                user_info={"username": row.get("user", "")},
            ),
            tags=[t for t in str(row.get("tags", "")).split(",") if t],
        )

    async def send_result(self, result: AnalysisResult) -> bool:
        event_id = result.metadata.get("source_alert_id")
        if not event_id:
            log.warning("splunk_send_skipped_no_event_id", alert_id=result.alert_id)
            return False
        session = await self._get_session()
        payload = {
            "ruleUIDs": [event_id],
            "comment": (
                f"[AlertTriage AI] Verdict: {result.verdict.value} "
                f"(confidence: {result.confidence:.0%})\n\n"
                f"{result.summary}\n\n"
                f"Reasoning: {result.reasoning[:500]}"
            ),
            "status": "1" if result.verdict.value == "true_positive" else "4",
        }
        try:
            r = await session.post(f"{self._base}/notable_update", json=payload)
            return r.status_code in (200, 201)
        except httpx.HTTPError as exc:
            log.error("splunk_send_result_failed", error=str(exc))
            return False
