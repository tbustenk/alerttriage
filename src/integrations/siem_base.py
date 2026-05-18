"""Abstract base class for all SIEM connectors."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from alerttriage.src.core.alert_models import Alert
from alerttriage.src.core.result_models import AnalysisResult


class SIEMConnector(ABC):
    """
    Pluggable SIEM integration interface.

    Implementors must provide:
      - fetch_alerts(): pull new alerts from the SIEM.
      - send_result(): write the AI verdict back to the SIEM.
      - health_check(): verify connectivity.

    Optional hooks (override as needed):
      - enrich_alert(): attach additional context before triage.
      - acknowledge_alert(): mark an alert as in-progress.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    @abstractmethod
    async def fetch_alerts(
        self,
        *,
        limit: int = 100,
        since_id: str | None = None,
    ) -> list[Alert]:
        """Pull unprocessed alerts from the SIEM."""

    @abstractmethod
    async def send_result(self, result: AnalysisResult) -> bool:
        """Write verdict and reasoning back to the SIEM. Returns True on success."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True if the SIEM is reachable and credentials are valid."""

    async def enrich_alert(self, alert: Alert) -> Alert:
        """Optionally attach extra context. Default: no-op."""
        return alert

    async def acknowledge_alert(self, alert_id: str) -> bool:
        """Mark an alert as being processed. Default: no-op."""
        return True

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} config={list(self.config)}>"
