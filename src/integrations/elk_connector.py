"""Elasticsearch / ELK Stack connector (SIEM detection alerts)."""

from __future__ import annotations

from typing import Any

import httpx

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


class ELKConnector(SIEMConnector):
    """
    Reads signals from Elasticsearch SIEM (Kibana Detection Engine API).

    Required config keys:
      host, port, api_key (base64 id:key), client_id
    Optional:
      index (.siem-signals-* default), verify_ssl, max_signals
    """

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._base = f"https://{config['host']}:{config.get('port', 9200)}"
        self._kibana_base = (
            f"https://{config['host']}:{config.get('kibana_port', 5601)}"
        )
        self._api_key = config["api_key"]
        self._verify = config.get("verify_ssl", True)
        self._client_id = config["client_id"]
        self._index = config.get("index", ".siem-signals-*")
        self._session: httpx.AsyncClient | None = None

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
                timeout=30.0,
            )
        return self._session

    async def health_check(self) -> bool:
        session = await self._get_session()
        try:
            r = await session.get(f"{self._base}/_cluster/health")
            return r.status_code == 200
        except Exception as exc:
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
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"signal.status": "open"}},
                    ]
                }
            },
        }
        if since_id:
            query["search_after"] = [since_id]

        try:
            r = await session.post(
                f"{self._base}/{self._index}/_search",
                json=query,
            )
            r.raise_for_status()
            hits = r.json().get("hits", {}).get("hits", [])
            return [self._parse_signal(h) for h in hits]
        except Exception as exc:
            log.error("elk_fetch_failed", error=str(exc))
            return []

    def _parse_signal(self, hit: dict[str, Any]) -> Alert:
        src = hit.get("_source", {})
        signal = src.get("signal", {})
        rule = signal.get("rule", {})
        sev = str(rule.get("severity", "medium")).lower()
        return Alert(
            client_id=self._client_id,
            source=AlertSource.ELK,
            source_alert_id=hit.get("_id"),
            rule_name=rule.get("name", "unknown"),
            severity=_SEVERITY_MAP.get(sev, AlertSeverity.MEDIUM),
            title=rule.get("name", "ELK Signal"),
            description=rule.get("description", ""),
            raw_payload=src,
            context=AlertContext(
                host_info={"hostname": src.get("host", {}).get("name", "")},
                user_info={"username": src.get("user", {}).get("name", "")},
            ),
            tags=rule.get("tags", []),
        )

    async def send_result(self, result: AnalysisResult) -> bool:
        source_id = result.metadata.get("source_alert_id")
        if not source_id:
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
        except Exception as exc:
            log.error("elk_send_result_failed", error=str(exc))
            return False
