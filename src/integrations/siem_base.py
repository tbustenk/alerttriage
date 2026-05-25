"""Abstract base class for all SIEM connectors."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from alerttriage.src.core.alert_models import Alert
from alerttriage.src.core.result_models import AnalysisResult


class ConfigValidationError(ValueError):
    """Raised when a connector's configuration is missing required keys or has invalid values."""


class SIEMConnectionError(OSError):
    """Raised when the SIEM cannot be reached after all retry attempts are exhausted."""


class SIEMConnector(ABC):
    """
    Pluggable SIEM integration interface.

    Implementors must provide:
      - fetch_alerts(): pull new alerts from the SIEM.
      - send_result(): write the AI verdict back to the SIEM.
      - health_check(): verify connectivity and credentials.
      - get_field_names(): schema discovery for a given index.

    Optional hooks (override as needed):
      - enrich_alert(): attach additional context before triage.
      - acknowledge_alert(): mark an alert as in-progress.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> None:
        """Raise :class:`ConfigValidationError` if required keys are absent or invalid.

        Subclasses should call ``super().validate_config(config)`` and then
        check their own required fields.
        """

    @abstractmethod
    async def fetch_alerts(
        self,
        *,
        limit: int = 100,
        since_id: str | None = None,
        time_range: str | None = None,
    ) -> list[Alert]:
        """Pull unprocessed alerts from the SIEM.

        Args:
            limit: Maximum number of alerts to return.
            since_id: Cursor / search-after value from a previous call.
            time_range: Connector-specific time range string (e.g. ``"-15m"``
                for Splunk, ``"now-15m"`` for ELK). Overrides the config default.
        """

    @abstractmethod
    async def send_result(self, result: AnalysisResult) -> bool:
        """Write verdict and reasoning back to the SIEM. Returns True on success."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True if the SIEM is reachable and credentials are valid."""

    @abstractmethod
    async def get_field_names(self, index: str) -> list[str]:
        """Return a sorted list of field names available in *index*.

        Raises:
            SIEMConnectionError: if the SIEM cannot be reached.
        """

    async def enrich_alert(self, alert: Alert) -> Alert:
        """Optionally attach extra context. Default: no-op."""
        return alert

    async def acknowledge_alert(self, alert_id: str) -> bool:
        """Mark an alert as being processed. Default: no-op."""
        return True

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} config={list(self.config)}>"
