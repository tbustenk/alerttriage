"""Feature flag manager for AlertTriage.

Flags are defined in ``config/features.yaml`` (global defaults) and can be
overridden per-client in the client config under the ``features`` key.

Example features.yaml:

  analytics:
    enabled: true
    description: "Advanced analytics and ROI reporting"
  webhooks:
    enabled: true
    description: "Outbound webhook notifications"
  pdf_export:
    enabled: true
    description: "PDF report export (requires fpdf2)"
  auto_dismiss:
    enabled: false
    description: "Auto-dismiss alerts below confidence threshold"
  hot_reload:
    enabled: true
    description: "Hot-reload config on file change"

Per-client override in client_config.yaml:

  features:
    auto_dismiss:
      enabled: true
      confidence_threshold: 0.95
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from alerttriage.src.logger import get_logger

log = get_logger(__name__)

_FEATURES_FILE = "features.yaml"


class FeatureFlagManager:
    """Read-through feature flag manager with per-client overrides.

    Args:
        config_dir:  Directory containing ``features.yaml``.
        config:      ConfigManager (to read per-client feature overrides).
    """

    def __init__(self, config_dir: Path, config: Any) -> None:
        self._config_dir = Path(config_dir)
        self._config = config
        self._global: dict[str, Any] = self._load_global()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_enabled(self, flag: str, client_id: str | None = None) -> bool:
        """Return True if *flag* is enabled globally or for *client_id*.

        Resolution order (later wins):
          1. Global default (``features.yaml``)
          2. ``ALERTTRIAGE_FEATURE_<FLAG>=true/false`` env override
          3. Per-client ``features.<flag>.enabled`` in client config
        """
        # Global default
        global_cfg = self._global.get(flag, {})
        enabled = bool(global_cfg.get("enabled", False))

        # Environment override
        env_key = f"ALERTTRIAGE_FEATURE_{flag.upper()}"
        env_val = os.environ.get(env_key)
        if env_val is not None:
            enabled = env_val.lower() in ("1", "true", "yes")

        # Per-client override
        if client_id:
            client_features = self._client_features(client_id)
            client_flag = client_features.get(flag, {})
            if isinstance(client_flag, dict) and "enabled" in client_flag:
                enabled = bool(client_flag["enabled"])
            elif isinstance(client_flag, bool):
                enabled = client_flag

        return enabled

    def get(self, flag: str, client_id: str | None = None) -> dict[str, Any]:
        """Return full flag config (``enabled`` + any extra keys) for a client."""
        global_cfg = dict(self._global.get(flag, {}))
        if client_id:
            client_features = self._client_features(client_id)
            override = client_features.get(flag, {})
            if isinstance(override, dict):
                global_cfg.update(override)
            elif isinstance(override, bool):
                global_cfg["enabled"] = override
        env_key = f"ALERTTRIAGE_FEATURE_{flag.upper()}"
        env_val = os.environ.get(env_key)
        if env_val is not None:
            global_cfg["enabled"] = env_val.lower() in ("1", "true", "yes")
        return global_cfg

    def list_flags(self, client_id: str | None = None) -> dict[str, dict[str, Any]]:
        """Return all flags with effective values for *client_id*."""
        return {flag: self.get(flag, client_id=client_id) for flag in self._global}

    def reload(self) -> None:
        """Reload global flags from disk (called on hot-reload)."""
        self._global = self._load_global()
        log.info("feature_flags_reloaded")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_global(self) -> dict[str, Any]:
        path = self._config_dir / _FEATURES_FILE
        if not path.exists():
            return _DEFAULT_FLAGS.copy()
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return {**_DEFAULT_FLAGS, **raw}
        except Exception as exc:  # noqa: BLE001
            log.error("feature_flags_load_failed", path=str(path), error=str(exc))
            return _DEFAULT_FLAGS.copy()

    def _client_features(self, client_id: str) -> dict[str, Any]:
        try:
            client_cfg = self._config.get_client(client_id)
            return client_cfg.get("features") or {}
        except Exception:  # noqa: BLE001
            return {}


_DEFAULT_FLAGS: dict[str, Any] = {
    "analytics":     {"enabled": True,  "description": "Advanced analytics and ROI reporting"},
    "webhooks":      {"enabled": True,  "description": "Outbound webhook notifications"},
    "pdf_export":    {"enabled": True,  "description": "PDF report export (requires fpdf2)"},
    "auto_dismiss":  {"enabled": False, "description": "Auto-dismiss alerts below confidence threshold"},
    "hot_reload":    {"enabled": True,  "description": "Hot-reload config on file change"},
    "scheduled_reports": {"enabled": True, "description": "Scheduled report email delivery"},
    "config_versioning": {"enabled": True, "description": "Config snapshot and rollback"},
}
