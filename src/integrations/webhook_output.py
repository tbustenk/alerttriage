"""Generic webhook output — pushes results to SOAR, ticketing, or custom endpoints."""

from __future__ import annotations

import hmac
import hashlib
import json
from typing import Any

import httpx

from alerttriage.src.core.alert_models import Alert
from alerttriage.src.core.result_models import AnalysisResult
from alerttriage.src.integrations.siem_base import SIEMConnector
from alerttriage.src.logger import get_logger

log = get_logger(__name__)


class WebhookOutput(SIEMConnector):
    """
    Sends AnalysisResult payloads to an arbitrary HTTPS endpoint.

    Required config keys:
      url, client_id
    Optional:
      secret (HMAC-SHA256 signing secret), headers (extra headers dict),
      timeout_sec, retry_count
    """

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._url: str = config["url"]
        self._secret: str | None = config.get("secret")
        self._extra_headers: dict[str, str] = config.get("headers", {})
        self._timeout = config.get("timeout_sec", 10.0)
        self._retries = config.get("retry_count", 2)
        self._client_id = config["client_id"]

    async def health_check(self) -> bool:
        async with httpx.AsyncClient(timeout=self._timeout) as session:
            try:
                r = await session.head(self._url)
                return r.status_code < 500
            except Exception:
                return False

    async def fetch_alerts(self, *, limit: int = 100, since_id: str | None = None) -> list[Alert]:
        """WebhookOutput is write-only; fetching is not supported."""
        return []

    async def send_result(self, result: AnalysisResult) -> bool:
        payload = result.model_dump(mode="json")
        body = json.dumps(payload, default=str)
        headers = {**self._extra_headers, "Content-Type": "application/json"}

        if self._secret:
            sig = hmac.new(
                self._secret.encode(), body.encode(), hashlib.sha256
            ).hexdigest()
            headers["X-AlertTriage-Signature"] = f"sha256={sig}"

        async with httpx.AsyncClient(timeout=self._timeout) as session:
            for attempt in range(1 + self._retries):
                try:
                    r = await session.post(self._url, content=body, headers=headers)
                    if r.status_code < 300:
                        log.info("webhook_sent", url=self._url, status=r.status_code)
                        return True
                    log.warning("webhook_non2xx", status=r.status_code, attempt=attempt)
                except Exception as exc:
                    log.warning("webhook_error", error=str(exc), attempt=attempt)
        return False
