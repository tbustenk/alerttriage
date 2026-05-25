"""Elasticsearch / ELK Stack connector (SIEM detection alerts).

Reads from the Elasticsearch ``_search`` API and writes statuses back via
the Kibana Detection Engine API. Holds a long-lived
:class:`httpx.AsyncClient` so the TLS handshake is amortised.

All network calls go through :func:`alerttriage.src.retry.with_retry`.

Authentication
--------------
Choose one auth strategy in your config:

1. **API key** (recommended): base64-encoded ``id:key`` string.
   Set via env var ``ALERTTRIAGE_<CLIENT_ID_UPPER>_API_KEY``
   or ``config["api_key"]``.

2. **Basic auth**: set ``config["username"]`` and supply the password
   via env var ``ALERTTRIAGE_<CLIENT_ID_UPPER>_ES_PASSWORD``.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from alerttriage.src.core.alert_models import Alert, AlertContext, AlertSeverity, AlertSource
from alerttriage.src.core.result_models import AnalysisResult
from alerttriage.src.integrations.siem_base import (
    ConfigValidationError,
    SIEMConnectionError,
    SIEMConnector,
)
from alerttriage.src.logger import get_logger
from alerttriage.src.retry import RateLimitedError, SimpleRetryConfig, TransientBackendError, with_retry

log = get_logger(__name__)

_SEVERITY_MAP: dict[str, AlertSeverity] = {
    "critical": AlertSeverity.CRITICAL,
    "high": AlertSeverity.HIGH,
    "medium": AlertSeverity.MEDIUM,
    "low": AlertSeverity.LOW,
    "informational": AlertSeverity.INFO,
    "info": AlertSeverity.INFO,
}


def _resolve_api_key(config: dict[str, Any]) -> str | None:
    if config.get("api_key"):
        return str(config["api_key"])
    client_id = str(config.get("client_id", "")).upper().replace("-", "_")
    return os.environ.get(f"ALERTTRIAGE_{client_id}_API_KEY")


def _resolve_es_password(config: dict[str, Any]) -> str | None:
    if config.get("password"):
        return str(config["password"])
    client_id = str(config.get("client_id", "")).upper().replace("-", "_")
    return os.environ.get(f"ALERTTRIAGE_{client_id}_ES_PASSWORD")


def _raise_for_siem_status(r: httpx.Response) -> None:
    if r.status_code == 429:
        retry_after: float | None = None
        raw = r.headers.get("Retry-After") or r.headers.get("retry-after")
        if raw:
            try:
                retry_after = float(raw)
            except ValueError:
                pass
        raise RateLimitedError("Elasticsearch rate-limited (429)", retry_after=retry_after)
    if r.status_code >= 500:
        raise TransientBackendError(f"Elasticsearch server error {r.status_code}: {r.text[:200]}")
    r.raise_for_status()


def _classify_httpx(exc: httpx.HTTPError) -> Exception:
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError)):
        return TransientBackendError(str(exc))
    return exc


class ELKConnector(SIEMConnector):
    """Reads signals from Elasticsearch SIEM (Kibana Detection Engine API).

    Required config keys:
      ``host``, ``client_id``. At least one auth method (see module docstring).

    Optional config keys:
      ``port`` (9200), ``kibana_port`` (5601), ``verify_ssl`` (True),
      ``index`` (``.siem-signals-*``), ``timeout_sec`` (30),
      ``time_range`` (``"now-15m"``),
      ``retry_max_attempts`` (3), ``retry_initial_delay_sec`` (0.5),
      ``retry_max_delay_sec`` (30), ``retry_factor`` (2.0), ``retry_jitter`` (True).
    """

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> None:
        missing = [k for k in ("host", "client_id") if not config.get(k)]
        if missing:
            raise ConfigValidationError(
                f"ELKConnector requires config keys: {', '.join(missing)}"
            )
        api_key = _resolve_api_key(config)
        password = _resolve_es_password(config)
        username = config.get("username")
        if not api_key and not (username and password):
            client_id = str(config.get("client_id", "")).upper().replace("-", "_")
            raise ConfigValidationError(
                f"ELKConnector needs auth. Set ALERTTRIAGE_{client_id}_API_KEY "
                "or set config 'username' + ALERTTRIAGE_{client_id}_ES_PASSWORD."
            )

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        if "host" not in config:
            raise ValueError("ELKConnector requires 'host' in config")
        if "client_id" not in config:
            raise ValueError("ELKConnector requires 'client_id' in config")
        self._base = f"https://{config['host']}:{config.get('port', 9200)}"
        self._kibana_base = f"https://{config['host']}:{config.get('kibana_port', 5601)}"
        self._api_key = _resolve_api_key(config)
        self._username = config.get("username")
        self._password = _resolve_es_password(config)
        self._verify = bool(config.get("verify_ssl", True))
        self._timeout = float(config.get("timeout_sec", 30.0))
        self._client_id = str(config["client_id"])
        self._index = str(config.get("index", ".siem-signals-*"))
        self._default_time_range = str(config.get("time_range", "now-15m"))
        self._retry = SimpleRetryConfig.from_dict(config)
        self._session: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _build_auth(self) -> httpx.Auth | None:
        if self._username and self._password:
            return httpx.BasicAuth(self._username, self._password)
        return None

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "kbn-xsrf": "true",
            "Content-Type": "application/json",
        }
        if self._api_key:
            headers["Authorization"] = f"ApiKey {self._api_key}"
        elif not (self._username and self._password):
            log.warning("elk_no_auth", client=self._client_id)
        return headers

    async def _get_session(self) -> httpx.AsyncClient:
        if self._session is None or self._session.is_closed:
            self._session = httpx.AsyncClient(
                headers=self._headers(),
                auth=self._build_auth(),
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
    # SIEMConnector implementation
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        session = await self._get_session()

        async def _check() -> bool:
            try:
                r = await session.get(f"{self._base}/_cluster/health")
            except httpx.HTTPError as exc:
                raise _classify_httpx(exc) from exc
            _raise_for_siem_status(r)
            body = r.json()
            status = body.get("status", "red")
            log.info("elk_cluster_health", status=status, client=self._client_id)
            return status in ("green", "yellow")

        try:
            return await with_retry(_check, config=self._retry, op_name="elk_health")
        except (RateLimitedError, TransientBackendError) as exc:
            log.warning("elk_health_exhausted", error=str(exc))
            return False

    async def fetch_alerts(
        self,
        *,
        limit: int = 100,
        since_id: str | None = None,
        time_range: str | None = None,
    ) -> list[Alert]:
        session = await self._get_session()
        gte = time_range or self._default_time_range
        query: dict[str, Any] = {
            "size": limit,
            "sort": [{"@timestamp": "desc"}],
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"signal.status": "open"}},
                        {"range": {"@timestamp": {"gte": gte}}},
                    ]
                }
            },
        }
        if since_id:
            query["search_after"] = [since_id]

        async def _fetch() -> list[Alert]:
            try:
                r = await session.post(
                    f"{self._base}/{self._index}/_search", json=query
                )
            except httpx.HTTPError as exc:
                raise _classify_httpx(exc) from exc
            _raise_for_siem_status(r)
            hits = (r.json() or {}).get("hits", {}).get("hits", []) or []
            alerts: list[Alert] = []
            for hit in hits:
                try:
                    alerts.append(self._parse_signal(hit))
                except Exception as exc:  # noqa: BLE001
                    log.warning("elk_parse_failed", error=str(exc))
            return alerts

        try:
            return await with_retry(_fetch, config=self._retry, op_name="elk_fetch")
        except (RateLimitedError, TransientBackendError) as exc:
            log.error("elk_fetch_exhausted", error=str(exc))
            return []

    def _parse_signal(self, hit: dict[str, Any]) -> Alert:
        src = hit.get("_source", {}) or {}
        signal = src.get("signal", {}) or {}
        rule = signal.get("rule", {}) or {}
        sev = str(rule.get("severity", "medium")).lower()

        host = src.get("host") or {}
        user = src.get("user") or {}
        process = src.get("process") or {}
        network = src.get("network") or {}
        event = src.get("event") or {}

        host_info: dict[str, Any] = {
            "hostname": host.get("name", ""),
            "ip": next(iter(host.get("ip", []) or []), ""),
            "os": (host.get("os") or {}).get("name", ""),
        }
        user_info: dict[str, Any] = {
            "username": user.get("name", ""),
            "domain": user.get("domain", ""),
        }
        additional: dict[str, Any] = {}
        if process:
            additional["process"] = {
                "name": process.get("name", ""),
                "pid": process.get("pid"),
                "args": process.get("args", []),
            }
        if network:
            additional["network"] = {
                "direction": network.get("direction", ""),
                "protocol": network.get("protocol", ""),
                "destination_ip": (src.get("destination") or {}).get("ip", ""),
                "destination_port": (src.get("destination") or {}).get("port"),
            }
        if event:
            additional["event"] = {
                "action": event.get("action", ""),
                "category": event.get("category", []),
                "outcome": event.get("outcome", ""),
            }

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
                host_info=host_info,
                user_info=user_info,
                additional_context=additional,
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

        async def _send() -> bool:
            try:
                r = await session.post(
                    f"{self._kibana_base}/api/detection_engine/signals/status",
                    json=payload,
                )
            except httpx.HTTPError as exc:
                raise _classify_httpx(exc) from exc
            _raise_for_siem_status(r)
            return r.status_code == 200

        try:
            return await with_retry(_send, config=self._retry, op_name="elk_send_result")
        except (RateLimitedError, TransientBackendError) as exc:
            log.error("elk_send_result_exhausted", error=str(exc))
            return False

    async def get_field_names(self, index: str = ".siem-signals-*") -> list[str]:
        """Return sorted field names from the Elasticsearch field capabilities API.

        Uses ``GET /<index>/_field_caps?fields=*`` which works on ES 7.x and 8.x.

        Raises:
            SIEMConnectionError: if the cluster cannot be reached.
        """
        session = await self._get_session()

        async def _fetch_fields() -> list[str]:
            try:
                r = await session.get(
                    f"{self._base}/{index}/_field_caps",
                    params={"fields": "*", "include_unmapped": "false"},
                )
            except httpx.HTTPError as exc:
                raise _classify_httpx(exc) from exc
            _raise_for_siem_status(r)
            body = r.json() or {}
            raw_fields = list((body.get("fields") or {}).keys())
            # Strip internal ES meta-fields and sort
            return sorted(f for f in raw_fields if not f.startswith("_"))

        try:
            return await with_retry(_fetch_fields, config=self._retry, op_name="elk_field_names")
        except (RateLimitedError, TransientBackendError) as exc:
            raise SIEMConnectionError(
                f"Could not retrieve ELK field names for index '{index}': {exc}"
            ) from exc
