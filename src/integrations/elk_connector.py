"""Elasticsearch / ELK Stack connector (SIEM detection alerts).

Reads from the Elasticsearch ``_search`` API and writes statuses back via
the Kibana Detection Engine API. Holds a long-lived
:class:`httpx.AsyncClient` so the TLS handshake is amortised.
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


class ELKConnector(SIEMConnector):
    """Reads signals from Elasticsearch SIEM (Kibana Detection Engine API).

    Required config keys:
      ``host``, ``api_key`` (base64 ``id:key``), ``client_id``.
    Optional config keys:
      ``port`` (9200), ``kibana_port`` (5601), ``verify_ssl`` (True),
      ``index`` (``.siem-signals-*``), ``timeout_sec`` (30.0).
    """

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        for required in ("host", "api_key", "client_id"):
            if required not in config:
                raise ValueError(f"ELKConnector requires a '{required}' in its config")
        self._base = f"https://{config['host']}:{config.get('port', 9200)}"
        self._kibana_base = f"https://{config['host']}:{config.get('kibana_port', 5601)}"
        self._api_key = str(config["api_key"])
        self._verify = bool(config.get("verify_ssl", True))
        self._timeout = float(config.get("timeout_sec", 30.0))
        self._client_id = str(config["client_id"])
        self._index = str(config.get("index", ".siem-signals-*"))
        self._session: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"ApiKey {self._api_key}",
            "kbn-xsrf": "true",
            "Content-Type": "application/json",
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
            r = await session.get(f"{self._base}/_cluster/health")
            return r.status_code == 200
        except httpx.HTTPError as exc:
            log.warning("elk_health_check_failed", error=str(exc))
            return False

    async def fetch_alerts(
        self,
        *,
        limit: int = 100,
        since_id: str | None = None,
    ) -> list[Alert]:
        session = await self._get_session()
        query: dict[str, Any] = {
            "size": limit,
            "sort": [{"@timestamp": "desc"}],
            "query": {"bool": {"filter": [{"term": {"signal.status": "open"}}]}},
        }
        if since_id:
            query["search_after"] = [since_id]
        try:
            r = await session.post(f"{self._base}/{self._index}/_search", json=query)
            r.raise_for_status()
            hits = (r.json() or {}).get("hits", {}).get("hits", []) or []
        except httpx.HTTPError as exc:
            log.error("elk_fetch_failed", error=str(exc))
            return []
        except ValueError as exc:
            log.error("elk_fetch_invalid_json", error=str(exc))
            return []

        alerts: list[Alert] = []
        for hit in hits:
            try:
                alerts.append(self._parse_signal(hit))
            except Exception as exc:  # noqa: BLE001
                log.warning("elk_parse_failed", error=str(exc))
        return alerts

    def _parse_signal(self, hit: dict[str, Any]) -> Alert:
        src = hit.get("_source", {}) or {}
        signal = src.get("signal", {}) or {}
        rule = signal.get("rule", {}) or {}
        sev = str(rule.get("severity", "medium")).lower()
        return Alert(
            client_id=self._client_id,
            source=AlertSource.ELK,
            source_alert_id=hit.get("_id"),
            rule_name=str(rule.get("name", "unknown")),
            severity=_SEVERITY_MAP.get(sev, AlertSeverity.MEDIUM),
            title=str(rule.get("name", "ELK Signal")),
            description=str(rule.get("description", "")),
            raw_payload=src,
            context=AlertContext(
                host_info={"hostname": (src.get("host") or {}).get("name", "")},
                user_info={"username": (src.get("user") or {}).get("name", "")},
            ),
            tags=list(rule.get("tags") or []),
        )

    async def send_result(self, result: AnalysisResult) -> bool:
        source_id = result.metadata.get("source_alert_id")
        if not source_id:
            log.warning("elk_send_skipped_no_id", alert_id=result.alert_id)
            return False
        session = await self._get_session()
        status = "closed" if result.verdict.value == "false_positive" else "acknowledged"
        payload = {
            "signal_ids": [source_id],
            "status": status,
            "comment": (
                f"[AlertTriage AI] {result.verdict.value} "
                f"(confidence {result.confidence:.0%}): {result.summary}"
            ),
        }
        try:
            r = await session.post(
                f"{self._kibana_base}/api/detection_engine/signals/status",
                json=payload,
            )
            return r.status_code == 200
        except httpx.HTTPError as exc:
            log.error("elk_send_result_failed", error=str(exc))
            return False
