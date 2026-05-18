"""Loads, validates, and merges YAML configuration files."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Pydantic validation schemas
# ---------------------------------------------------------------------------

class CostLimitsConfig(BaseModel):
    daily_usd: float | None = None
    monthly_usd: float | None = None


class ClientConfig(BaseModel):
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


class ModelSpec(BaseModel):
    provider: str
    model_name: str
    max_tokens: int = 1024
    temperature: float = 0.2


class ModelsConfig(BaseModel):
    default: str
    models: dict[str, ModelSpec]


class DefaultConfig(BaseModel):
    log_level: str = "INFO"
    json_logs: bool = False
    data_dir: str = "data"
    reports_dir: str = "reports"
    anonymize_salt: str = "change-me-in-production"
    default_model: str = "claude-sonnet"


# ---------------------------------------------------------------------------

class ConfigManager:
    """
    Single source of truth for all runtime configuration.

    Loading order (later entries override earlier):
      1. config/default_config.yaml
      2. Environment variables (ALERTTRIAGE_*)
      3. config/client_configs/<client_id>.yaml  (per-client)
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

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    @property
    def log_level(self) -> str:
        return os.environ.get("ALERTTRIAGE_LOG_LEVEL", self._default.log_level)

    @property
    def json_logs(self) -> bool:
        return os.environ.get("ALERTTRIAGE_JSON_LOGS", "").lower() in ("1", "true")

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
        return self._models.get("default", "claude-sonnet")

    @property
    def models_config(self) -> dict[str, Any]:
        return self._models

    def get_client(self, client_id: str) -> dict[str, Any]:
        """Return merged client config dict, falling back to defaults."""
        path = self._config_dir / "client_configs" / f"{client_id}.yaml"
        if not path.exists():
            return {"client_id": client_id, "model": self.default_model_id}
        raw = _load_yaml(path)
        # Allow env-var overrides for sensitive fields
        env_key = client_id.upper().replace("-", "_")
        if key := os.environ.get(f"ALERTTRIAGE_{env_key}_API_KEY"):
            raw.setdefault("siem", {})["api_key"] = key
        return raw

    def list_clients(self) -> list[str]:
        client_dir = self._config_dir / "client_configs"
        if not client_dir.exists():
            return []
        return [p.stem for p in client_dir.glob("*.yaml") if p.stem != "template"]

    # ------------------------------------------------------------------
    # Internal loaders
    # ------------------------------------------------------------------

    def _load_default(self) -> DefaultConfig:
        path = self._config_dir / "default_config.yaml"
        raw = _load_yaml(path) if path.exists() else {}
        return DefaultConfig(**raw)

    def _load_models(self) -> dict[str, Any]:
        path = self._config_dir / "models.yaml"
        return _load_yaml(path) if path.exists() else {"default": "claude-sonnet", "models": {}}


def _load_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data or {}
