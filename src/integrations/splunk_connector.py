"""Splunk Enterprise Security connector (REST API v2).

Reads notable events and writes verdicts back as comments on the notable.
Holds a long-lived :class:`httpx.AsyncClient` so token authentication and
TLS handshakes are reused across requests.

All network calls go through :func:`alerttriage.src.retry.with_retry` so
transient failures and rate limits are handled uniformly.

Authentication
--------------
The connector looks for the bearer token in this order:

1. ``config["token"]`` or ``config["api_key"]`` (not recommended for production)
2. Environment variable ``ALERTTRIAGE_<CLIENT_ID_UPPER>_API_KEY``

Never commit the token to a YAML file.
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

_DEFAULT_SEARCH = "| inputlookup notable | where status=0"


def _resolve_token(config: dict[str, Any]) -> str | None:
    """Return the bearer token from config or environment variable."""
    if config.get("token"):
        return str(config["token"])
    if config.get("api_key"):
        return str(config["api_key"])
    client_id = str(config.get("client_id", "")).upper().replace("-", "_")
    env_key = f"ALERTTRIAGE_{client_id}_API_KEY"
    return os.environ.get(env_key)


def _raise_for_siem_status(r: httpx.Response) -> None:
    """Convert HTTP error codes to retry-aware exceptions."""
    if r.status_code == 429:
        retry_after: float | None = None
        raw = r.headers.get("Retry-After") or r.headers.get("retry-after")
        if raw:
            try:
                retry_after = float(raw)
            except ValueError:
                pass
        raise RateLimitedError(f"Splunk rate-limited (429)", retry_after=retry_after)
    if r.status_code >= 500:
        raise TransientBackendError(f"Splunk server error {r.status_code}: {r.text[:200]}")
    r.raise_for_status()


def _classify_httpx(exc: httpx.HTTPError) -> Exception:
    """Map an httpx transport error to a retry primitive."""
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError)):
        return TransientBackendError(str(exc))
    return exc


class SplunkConnector(SIEMConnector):
    """Reads notable events from Splunk ES and writes verdicts back as comments.

    Required config keys:
      ``host``, ``client_id``.  Token via env var (see module docstring).

    Optional config keys:
      ``port`` (8089), ``verify_ssl`` (True), ``earliest_time`` ("-15m"),
      ``search_query``, ``index``, ``timeout_sec`` (30),
      ``retry_max_attempts`` (3), ``retry_initial_delay_sec`` (0.5),
      ``retry_max_delay_sec`` (30), ``retry_factor`` (2.0), ``retry_jitter`` (True).
    """

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> None:
        missing = [k for k in ("host", "client_id") if not config.get(k)]
        if missing:
            raise ConfigValidationError(
                f"SplunkConnector requires config keys: {', '.join(missing)}"
            )
        token = _resolve_token(config)
        if not token:
            client_id = str(config.get("client_id", "")).upper().replace("-", "_")
            raise ConfigValidationError(
                f"No bearer token found. Set env var ALERTTRIAGE_{client_id}_API_KEY "
                "or add 'token' to the config (not recommended for production)."
            )

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        if "host" not in config:
            raise ValueError("SplunkConnector requires 'host' in config")
        if "client_id" not in config:
            raise ValueError("SplunkConnector requires 'client_id' in config")
        self._base = f"https://{config['host']}:{config.get('port', 8089)}/services"
        self._token = _resolve_token(config)
        self._verify = bool(config.get("verify_ssl", True))
        self._timeout = float(config.get("timeout_sec", 30.0))
        self._client_id = str(config["client_id"])
        self._retry = SimpleRetryConfig.from_dict(config)
        self._session: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Session management
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
    # SIEMConnector implementation
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        session = await self._get_session()

        async def _check() -> bool:
            try:
                r = await session.get(f"{self._base}/server/info?output_mode=json")
            except httpx.HTTPError as exc:
                raise _classify_httpx(exc) from exc
            _raise_for_siem_status(r)
            return r.status_code == 200

        try:
            return await with_retry(_check, config=self._retry, op_name="splunk_health")
        except (RateLimitedError, TransientBackendError) as exc:
            log.warning("splunk_health_exhausted", error=str(exc))
            return False

    async def fetch_alerts(
        self,
        *,
        limit: int = 100,
        since_id: str | None = None,
        time_range: str | None = None,
    ) -> list[Alert]:
        session = await self._get_session()
        earliest = time_range or self.config.get("earliest_time", "-15m")
        search = self.config.get("search_query", _DEFAULT_SEARCH)
        params: dict[str, Any] = {
            "output_mode": "json",
            "count": limit,
            "earliest_time": earliest,
            "search": search,
        }
        if since_id:
            params["offset"] = since_id

        async def _fetch() -> list[Alert]:
            try:
                r = await session.get(f"{self._base}/search/jobs/export", params=params)
            except httpx.HTTPError as exc:
                raise _classify_httpx(exc) from exc
            _raise_for_siem_status(r)
            # Splunk export streams one JSON object per line
            results: list[dict[str, Any]] = []
            for line in r.text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    import json
                    obj = json.loads(line)
                    # Export endpoint wraps results in {"preview":false,"result":{...}}
                    if "result" in obj:
                        results.append(obj["result"])
                    elif "results" in obj:
                        results.extend(obj["results"])
                except (ValueError, KeyError):
                    pass

            alerts: list[Alert] = []
            for row in results:
                try:
                    alerts.append(self._parse_notable(row))
                except Exception as exc:  # noqa: BLE001
                    log.warning("splunk_parse_failed", error=str(exc))
            return alerts

        try:
            return await with_retry(_fetch, config=self._retry, op_name="splunk_fetch")
        except (RateLimitedError, TransientBackendError) as exc:
            log.error("splunk_fetch_exhausted", error=str(exc))
            return []

    def _parse_notable(self, row: dict[str, Any]) -> Alert:
        sev_str = str(row.get("urgency", "medium")).lower()
        tags_raw = row.get("tags", "") or ""
        tags = [t.strip() for t in str(tags_raw).split(",") if t.strip()]
        # Enrich host_info with as many fields as Splunk provides
        host_info: dict[str, Any] = {
            "hostname": row.get("dest", "") or row.get("dest_host", ""),
            "ip": row.get("dest_ip", "") or row.get("src_ip", ""),
        }
        if row.get("src"):
            host_info["src"] = row["src"]
        if row.get("src_host"):
            host_info["src_hostname"] = row["src_host"]

        user_info: dict[str, Any] = {"username": row.get("user", "") or row.get("src_user", "")}
        if row.get("user_category"):
            user_info["category"] = row["user_category"]

        return Alert(
            client_id=self._client_id,
            source=AlertSource.SPLUNK,
            rule_name=str(row.get("rule_name", "unknown")),
            severity=_SEVERITY_MAP.get(sev_str, AlertSeverity.MEDIUM),
            title=str(row.get("rule_title", row.get("rule_name", "Splunk Notable"))),
            description=str(row.get("rule_description", "")),
            source_alert_id=row.get("event_id") or row.get("_key"),
            raw_payload=row,
            context=AlertContext(
                host_info=host_info,
                user_info=user_info,
                additional_context={
                    k: v for k, v in row.items()
                    if k.startswith("mitre_") or k in ("category", "annotations")
                },
            ),
            tags=tags,
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
            # status: 0=unassigned, 1=assigned, 2=in progress, 3=pending, 4=resolved
            "status": "1" if result.verdict.value == "true_positive" else "4",
        }

        async def _send() -> bool:
            try:
                r = await session.post(f"{self._base}/notable_update", json=payload)
            except httpx.HTTPError as exc:
                raise _classify_httpx(exc) from exc
            _raise_for_siem_status(r)
            return r.status_code in (200, 201)

        try:
            return await with_retry(_send, config=self._retry, op_name="splunk_send_result")
        except (RateLimitedError, TransientBackendError) as exc:
            log.error("splunk_send_result_exhausted", error=str(exc))
            return False

    async def get_field_names(self, index: str = "notable") -> list[str]:
        """Return sorted field names by sampling recent events from *index*.

        Uses the Splunk ``fieldsummary`` SPL command on a small window.

        Raises:
            SIEMConnectionError: if the SIEM cannot be reached.
        """
        session = await self._get_session()
        search = f'search index="{index}" earliest=-1h | head 200 | fieldsummary | fields field'
        params: dict[str, Any] = {
            "output_mode": "json",
            "count": 500,
            "search": search,
        }

        async def _fetch_fields() -> list[str]:
            try:
                r = await session.get(f"{self._base}/search/jobs/export", params=params)
            except httpx.HTTPError as exc:
                raise _classify_httpx(exc) from exc
            _raise_for_siem_status(r)

            import json
            fields: list[str] = []
            for line in r.text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    row = obj.get("result") or {}
                    if "field" in row:
                        fields.append(str(row["field"]))
                except (ValueError, KeyError):
                    pass
            # Strip internal Splunk fields
            return sorted(f for f in fields if not f.startswith("_") or f == "_raw")

        try:
            return await with_retry(_fetch_fields, config=self._retry, op_name="splunk_field_names")
        except (RateLimitedError, TransientBackendError) as exc:
            raise SIEMConnectionError(
                f"Could not retrieve Splunk field names for index '{index}': {exc}"
            ) from exc
