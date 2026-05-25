"""Routes alert analysis requests to the appropriate AI backend.

Each backend is constructed once at startup from ``models.yaml``. ``route()``
picks the primary model from the client's config, then tries each fallback
in order if the primary call raises. Backends share a single retry policy
(see :mod:`alerttriage.src.retry`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from alerttriage.src.core.alert_models import Alert
from alerttriage.src.core.result_models import AnalysisResult
from alerttriage.src.logger import get_logger
from alerttriage.src.retry import RetryConfigLike

if TYPE_CHECKING:
    from alerttriage.config.config_manager import ConfigManager

log = get_logger(__name__)


class ModelUnavailableError(Exception):
    """Raised when all configured backends fail."""


class _Backend(Protocol):
    """Minimal protocol every backend must satisfy."""

    model_id: str

    async def analyze(
        self,
        alert: Alert,
        *,
        system_prompt: str,
        retry_config: RetryConfigLike | None,
    ) -> AnalysisResult: ...


class ModelRouter:
    """Selects and calls the right AI backend for a given client.

    Priority order:
      1. Client-level override in ``client_configs/<id>.yaml``.
      2. Global default from ``models.yaml``.
      3. Fallback chain (if configured).
    """

    def __init__(self, config: ConfigManager) -> None:
        self.config = config
        self._backends: dict[str, _Backend] = {}
        self._load_backends()

    # ------------------------------------------------------------------

    def _load_backends(self) -> None:
        from alerttriage.src.models.claude_backend import ClaudeBackend
        from alerttriage.src.models.gpt_backend import GPTBackend

        models_cfg = self.config.models_config

        for model_id, spec in (models_cfg.get("models") or {}).items():
            provider = str(spec.get("provider", "")).lower()
            if provider == "anthropic":
                self._backends[model_id] = ClaudeBackend(model_id, spec)
            elif provider == "openai":
                self._backends[model_id] = GPTBackend(model_id, spec)
            else:
                log.warning("unknown_provider", model_id=model_id, provider=provider)

        log.info("backends_loaded", count=len(self._backends), ids=list(self._backends))

    # ------------------------------------------------------------------

    async def route(
        self,
        alert: Alert,
        *,
        system_prompt: str,
        retry_config: RetryConfigLike | None = None,
    ) -> AnalysisResult:
        """Select backend(s) and run analysis, falling back on failure.

        Args:
            alert: Already-anonymised alert.
            system_prompt: The full system prompt to send to the model
                (typically produced by :class:`PromptEnhancer`).
            retry_config: Shared retry policy.

        Raises:
            ModelUnavailableError: When every configured backend has failed.
        """
        client_cfg: dict[str, Any] = self.config.get_client(alert.client_id)
        primary_id = client_cfg.get("model", self.config.default_model_id)
        fallback_ids: list[str] = list(client_cfg.get("model_fallbacks") or [])

        attempted: list[str] = []
        last_exc: BaseException | None = None
        for model_id in [primary_id, *fallback_ids]:
            backend = self._backends.get(model_id)
            if backend is None:
                log.warning("backend_not_found", model_id=model_id)
                continue
            attempted.append(model_id)
            try:
                log.info("calling_backend", model_id=model_id, alert_id=alert.id)
                result = await backend.analyze(
                    alert,
                    system_prompt=system_prompt,
                    retry_config=retry_config,
                )
                result.model_id = model_id
                return result
            except Exception as exc:  # noqa: BLE001 — fallback chain owns the policy
                last_exc = exc
                log.warning("backend_failed", model_id=model_id, error=str(exc))

        raise ModelUnavailableError(
            f"All backends failed for alert {alert.id}. Tried: {attempted}"
        ) from last_exc
