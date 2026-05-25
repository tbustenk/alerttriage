"""Generic webhook output — pushes results to SOAR, ticketing, or custom endpoints.

Write-only by design: ``fetch_alerts`` returns an empty list, since callers
that want a webhook *input* should run a separate ingestion service.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx

from alerttriage.src.core.alert_models import Alert
from alerttriage.src.core.result_models import AnalysisResult
from alerttriage.src.integrations.siem_base import SIEMConnector
from alerttriage.src.logger import get_logger

log = get_logger(__name__)


class WebhookOutput(SIEMConnector):
    """Sends ``AnalysisResult`` payloads to an arbitrary HTTPS endpoint.

    Required config keys:
      ``url``, ``client_id``
    Optional config keys:
      ``secret`` — HMAC-SHA256 signing secret (header ``X-AlertTriage-Signature``).
      ``headers`` — extra static request headers.
      ``timeout_sec``, ``retry_count``.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        if "url" not in config:
            raise ValueError("WebhookOutput requires a 'url' in its config")
        if "client_id" not in config:
            raise ValueError("WebhookOutput requires a 'client_id' in its config")
        self._url: str = str(config["url"])
        self._secret: str | None = config.get("secret")
        self._extra_headers: dict[str, str] = dict(config.get("headers") or {})
        self._timeout: float = float(config.get("timeout_sec", 10.0))
        self._retries: int = int(config.get("retry_count", 2))
        self._client_id: str = str(config["client_id"])

    async def health_check(self) -> bool:
        """``HEAD`` the endpoint; any non-5xx response counts as reachable."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as session:
                r = await session.head(self._url)
                return r.status_code < 500
        except httpx.HTTPError as exc:
            log.warning("webhook_health_check_failed", url=self._url, error=str(exc))
            return False

    async def fetch_alerts(self, *, limit: int = 100, since_id: str | None = None) -> list[Alert]:
        """Webhook output is write-only; returns an empty list."""
        return []

    async def send_result(self, result: AnalysisResult) -> bool:
        """POST the JSON payload, retrying transient failures up to ``retry_count``."""
        payload = result.model_dump(mode="json")
        body = json.dumps(payload, default=str)
        headers = {**self._extra_headers, "Content-Type": "application/json"}

        if self._secret:
            sig = hmac.new(self._secret.encode(), body.encode(), hashlib.sha256).hexdigest()
            headers["X-AlertTriage-Signature"] = f"sha256={sig}"

        last_status: int | None = None
        last_error: str | None = None
        async with httpx.AsyncClient(timeout=self._timeout) as session:
            for attempt in range(1 + self._retries):
                try:
                    r = await session.post(self._url, content=body, headers=headers)
                    last_status = r.status_code
                    if r.status_code < 300:
                        log.info("webhook_sent", url=self._url, status=r.status_code)
                        return True
                    if r.status_code < 500:
                        log.error(
                            "webhook_non_retryable",
                            url=self._url,
                            status=r.status_code,
                            body=r.text[:200],
                        )
                        return False
                    log.warning(
                        "webhook_retryable_status",
                        url=self._url,
                        status=r.status_code,
                        attempt=attempt,
                    )
                except httpx.HTTPError as exc:
                    last_error = str(exc)
                    log.warning("webhook_error", error=last_error, attempt=attempt)
        log.error(
            "webhook_failed",
            url=self._url,
            last_status=last_status,
            last_error=last_error,
        )
        return False
