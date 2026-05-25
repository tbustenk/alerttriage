"""Loads, validates, and merges YAML configuration files.

Loading order (later entries override earlier):
  1. ``config/default_config.yaml`` — system-wide defaults.
  2. Environment variables — ``ALERTTRIAGE_*``.
  3. ``config/client_configs/<client_id>.yaml`` — per-client overrides.

All loaded structures pass through Pydantic models, so a malformed config
fails fast at startup rather than at first use.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from alerttriage.src.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Pydantic validation schemas
# ---------------------------------------------------------------------------


class CostLimitsConfig(BaseModel):
    """Daily and monthly spend caps in USD; ``None`` disables a limit."""

    daily_usd: float | None = Field(default=None, ge=0.0)
    monthly_usd: float | None = Field(default=None, ge=0.0)


class ClientConfig(BaseModel):
    """Validated shape of a per-client YAML file."""

    client_id: str
    display_name: str = ""
    model: str = "claude-sonnet"
    model_fallbacks: list[str] = Field(default_factory=list)
    siem: dict[str, Any] = Field(default_factory=dict)
    cost_limits: CostLimitsConfig = Field(default_factory=CostLimitsConfig)
    webhook_url: str | None = None
    anonymize: bool = True
    tier: str = "standard"
    metadata: dict[str, Any] = Field(default_factory=dict)
    learning: dict[str, Any] = Field(default_factory=dict)

    @field_validator("client_id")
    @classmethod
    def client_id_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("client_id must not be blank")
        return v.strip().lower()


class ModelSpec(BaseModel):
    """One row in ``models.yaml``."""

    provider: str
    model_name: str
    max_tokens: int = Field(default=1024, gt=0)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)


class ModelsConfig(BaseModel):
    """The whole ``models.yaml`` document."""

    default: str
    models: dict[str, ModelSpec]


class ConcurrencyConfig(BaseModel):
    """How many backend calls may run in parallel."""

    max_in_flight: int = Field(default=5, gt=0)


class RetryConfig(BaseModel):
    """Exponential-backoff parameters shared across all backends."""

    max_attempts: int = Field(default=4, gt=0)
    initial_delay_sec: float = Field(default=0.5, gt=0.0)
    max_delay_sec: float = Field(default=30.0, gt=0.0)
    factor: float = Field(default=2.0, ge=1.0)
    jitter: bool = True


class LearningConfig(BaseModel):
    """Thresholds for the prompt-hint generator."""

    fp_rate_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    min_sample_size: int = Field(default=5, ge=0)
    max_hints: int = Field(default=10, gt=0)
    cache_ttl_sec: int = Field(default=300, ge=0)


class FeedbackConfig(BaseModel):
    """Bulk ingestion knobs for the feedback store."""

    batch_size: int = Field(default=200, gt=0)


class DefaultConfig(BaseModel):
    """The shape of ``default_config.yaml``."""

    log_level: str = "INFO"
    json_logs: bool = False
    data_dir: str = "data"
    reports_dir: str = "reports"
    anonymize_salt: str = "change-me-in-production"
    default_model: str = "claude-sonnet"
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    learning: LearningConfig = Field(default_factory=LearningConfig)
    feedback: FeedbackConfig = Field(default_factory=FeedbackConfig)


# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """Raised when a config file is missing required keys or malformed."""


class ConfigManager:
    """Single source of truth for all runtime configuration.

    Per-client configs are validated lazily on first access; the system-wide
    ``default_config.yaml`` and ``models.yaml`` are validated at construction.
    """

    def __init__(
        self,
        config_dir: Path | str = "config",
        *,
        client_id: str | None = None,
    ) -> None:
        self._config_dir = Path(config_dir)
        self._client_id = client_id
        self._default = self._load_default()
        self._models = self._load_models()
        self._client_cache: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # System-wide accessors
    # ------------------------------------------------------------------

    @property
    def log_level(self) -> str:
        return os.environ.get("ALERTTRIAGE_LOG_LEVEL", self._default.log_level)

    @property
    def json_logs(self) -> bool:
        env = os.environ.get("ALERTTRIAGE_JSON_LOGS")
        if env is not None:
            return env.lower() in ("1", "true", "yes")
        return self._default.json_logs

    @property
    def data_dir(self) -> Path:
        return Path(os.environ.get("ALERTTRIAGE_DATA_DIR", self._default.data_dir))

    @property
    def reports_dir(self) -> Path:
        return Path(os.environ.get("ALERTTRIAGE_REPORTS_DIR", self._default.reports_dir))

    @property
    def anonymize_salt(self) -> str:
        return os.environ.get("ALERTTRIAGE_ANONYMIZE_SALT", self._default.anonymize_salt)

    @property
    def default_model_id(self) -> str:
        return self._models.get("default", self._default.default_model)

    @property
    def models_config(self) -> dict[str, Any]:
        return self._models

    @property
    def concurrency(self) -> ConcurrencyConfig:
        return self._default.concurrency

    @property
    def retry(self) -> RetryConfig:
        return self._default.retry

    @property
    def learning(self) -> LearningConfig:
        return self._default.learning

    @property
    def feedback(self) -> FeedbackConfig:
        return self._default.feedback

    # ------------------------------------------------------------------
    # Client config
    # ------------------------------------------------------------------

    def get_client(self, client_id: str) -> dict[str, Any]:
        """Return a validated, cached client config dict.

        Falls back to a minimal stub when no YAML exists for the client so
        single-tenant or test callers don't blow up on a missing file.
        """
        if client_id in self._client_cache:
            return self._client_cache[client_id]

        path = self._config_dir / "client_configs" / f"{client_id}.yaml"
        if not path.exists():
            stub = {"client_id": client_id, "model": self.default_model_id}
            self._client_cache[client_id] = stub
            return stub

        raw = _load_yaml(path)
        try:
            validated = ClientConfig(**raw).model_dump(exclude_none=False)
        except ValidationError as exc:
            raise ConfigError(f"Invalid client config at {path}: {exc}") from exc

        env_key = client_id.upper().replace("-", "_")
        if key := os.environ.get(f"ALERTTRIAGE_{env_key}_API_KEY"):
            validated.setdefault("siem", {})["api_key"] = key
            validated.setdefault("siem", {})["token"] = key

        self._client_cache[client_id] = validated
        return validated

    def get_learning(self, client_id: str) -> LearningConfig:
        """Effective learning thresholds for a client (per-client overrides win)."""
        raw = self.get_client(client_id).get("learning") or {}
        merged = self._default.learning.model_dump()
        merged.update({k: v for k, v in raw.items() if v is not None})
        return LearningConfig(**merged)

    def list_clients(self) -> list[str]:
        client_dir = self._config_dir / "client_configs"
        if not client_dir.exists():
            return []
        return sorted(p.stem for p in client_dir.glob("*.yaml") if p.stem != "template")

    # ------------------------------------------------------------------
    # Internal loaders
    # ------------------------------------------------------------------

    def _load_default(self) -> DefaultConfig:
        path = self._config_dir / "default_config.yaml"
        raw = _load_yaml(path) if path.exists() else {}
        try:
            return DefaultConfig(**raw)
        except ValidationError as exc:
            raise ConfigError(f"Invalid {path}: {exc}") from exc

    def _load_models(self) -> dict[str, Any]:
        path = self._config_dir / "models.yaml"
        if not path.exists():
            return {"default": self._default.default_model, "models": {}}
        raw = _load_yaml(path)
        try:
            ModelsConfig(**raw)
        except ValidationError as exc:
            raise ConfigError(f"Invalid {path}: {exc}") from exc
        return raw


def _load_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML file and return a dict (empty if the file is empty)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse YAML {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"YAML root in {path} must be a mapping, got {type(data).__name__}")
    return data
