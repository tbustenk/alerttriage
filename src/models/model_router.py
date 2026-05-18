"""Routes alert analysis requests to the appropriate AI backend."""

from __future__ import annotations

from typing import TYPE_CHECKING

from alerttriage.src.core.alert_models import Alert
from alerttriage.src.core.result_models import AnalysisResult
from alerttriage.src.logger import get_logger

if TYPE_CHECKING:
    from alerttriage.config.config_manager import ConfigManager

log = get_logger(__name__)


class ModelUnavailableError(Exception):
    """Raised when all configured backends fail."""


class ModelRouter:
    """
    Selects and calls the right AI backend for a given client.

    Priority order:
      1. Client-level override in client config.
      2. Global default from models.yaml.
      3. Fallback chain (if enabled).
    """

    def __init__(self, config: "ConfigManager") -> None:
        self.config = config
        self._backends: dict[str, object] = {}
        self._load_backends()

    def _load_backends(self) -> None:
        from alerttriage.src.models.claude_backend import ClaudeBackend
        from alerttriage.src.models.gpt_backend import GPTBackend

        models_cfg = self.config.models_config

        for model_id, spec in models_cfg.get("models", {}).items():
            provider = spec.get("provider", "").lower()
            if provider == "anthropic":
                self._backends[model_id] = ClaudeBackend(model_id, spec)
            elif provider == "openai":
                self._backends[model_id] = GPTBackend(model_id, spec)
            else:
                log.warning("unknown_provider", model_id=model_id, provider=provider)

        log.info("backends_loaded", count=len(self._backends),
                 ids=list(self._backends))

    async def route(self, alert: Alert) -> AnalysisResult:
        """Select backend and run analysis, with fallback on failure."""
        client_cfg = self.config.get_client(alert.client_id)
        primary_id = client_cfg.get("model", self.config.default_model_id)
        fallback_ids: list[str] = client_cfg.get("model_fallbacks", [])

        for model_id in [primary_id, *fallback_ids]:
            backend = self._backends.get(model_id)
            if backend is None:
                log.warning("backend_not_found", model_id=model_id)
                continue
            try:
                log.info("calling_backend", model_id=model_id, alert_id=alert.id)
                result = await backend.analyze(alert)  # type: ignore[attr-defined]
                result.model_id = model_id
                return result
            except Exception as exc:
                log.warning("backend_failed", model_id=model_id, error=str(exc))

        raise ModelUnavailableError(
            f"All backends failed for alert {alert.id}. Tried: "
            f"{[primary_id, *fallback_ids]}"
        )
