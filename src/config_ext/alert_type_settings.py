"""Per-alert-type configuration settings.

Reads ``config/alert_types.yaml`` and resolves settings for a given rule name
using exact match → glob pattern → ``_defaults`` fallback.

Example usage:
    mgr = AlertTypeSettingsManager(config_dir, config)
    settings = mgr.get("brute_force_login", client_id="acme-corp")
    # → {"confidence_threshold": 0.70, "auto_dismiss": False, ...}
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

import yaml

from alerttriage.src.logger import get_logger

log = get_logger(__name__)

_ALERT_TYPES_FILE = "alert_types.yaml"

_GLOBAL_DEFAULTS: dict[str, Any] = {
    "confidence_threshold": 0.70,
    "auto_dismiss": False,
    "auto_dismiss_confidence": 0.97,
    "escalate_on_low_confidence": False,
    "max_latency_ms": 5000,
    "notes": "",
}


class AlertTypeSettingsManager:
    """Resolve per-alert-type config for any rule name.

    Args:
        config_dir: Directory containing ``alert_types.yaml``.
        config:     ConfigManager (for per-client ``alert_types`` overrides).
    """

    def __init__(self, config_dir: Path, config: Any) -> None:
        self._config_dir = Path(config_dir)
        self._config = config
        self._global = self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, rule_name: str, client_id: str | None = None) -> dict[str, Any]:
        """Return effective settings for *rule_name*, merging all layers.

        Resolution: _defaults → glob patterns → exact rule entry → per-client
        """
        base = {**_GLOBAL_DEFAULTS}

        # Apply _defaults from YAML
        base.update(self._global.get("_defaults", {}))

        # Apply matching glob patterns (in order)
        for pat_entry in self._global.get("_patterns", []):
            if fnmatch.fnmatch(rule_name, pat_entry.get("pattern", "")):
                merged = {k: v for k, v in pat_entry.items() if k != "pattern"}
                base.update(merged)

        # Apply exact rule entry
        if rule_name in self._global:
            exact = {k: v for k, v in self._global[rule_name].items() if not k.startswith("_")}
            base.update(exact)

        # Apply per-client overrides
        if client_id:
            base.update(self._client_override(rule_name, client_id))

        return base

    def list_rules(self) -> list[str]:
        """Return all explicitly configured rule names (excluding meta keys)."""
        return [k for k in self._global if not k.startswith("_")]

    def reload(self) -> None:
        self._global = self._load()
        log.info("alert_type_settings_reloaded")

    def update_rule(
        self,
        rule_name: str,
        settings: dict[str, Any],
        client_id: str | None = None,
    ) -> None:
        """Persist updated settings for *rule_name* (global or client-level)."""
        if client_id:
            # Store in the client YAML under alert_types.<rule_name>
            client_cfg_path = self._config_dir / "client_configs" / f"{client_id}.yaml"
            if client_cfg_path.exists():
                raw = yaml.safe_load(client_cfg_path.read_text(encoding="utf-8")) or {}
                raw.setdefault("alert_types", {})[rule_name] = settings
                client_cfg_path.write_text(yaml.safe_dump(raw, default_flow_style=False), encoding="utf-8")
                log.info("alert_type_setting_saved", rule=rule_name, client=client_id)
        else:
            path = self._config_dir / _ALERT_TYPES_FILE
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
            raw = raw or {}
            raw[rule_name] = settings
            path.write_text(yaml.safe_dump(raw, default_flow_style=False), encoding="utf-8")
            self._global = raw
            log.info("alert_type_setting_saved", rule=rule_name)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        path = self._config_dir / _ALERT_TYPES_FILE
        if not path.exists():
            return {}
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            log.error("alert_types_load_failed", error=str(exc))
            return {}

    def _client_override(self, rule_name: str, client_id: str) -> dict[str, Any]:
        try:
            client_cfg = self._config.get_client(client_id)
            at = client_cfg.get("alert_types") or {}
            return at.get(rule_name) or {}
        except Exception:  # noqa: BLE001
            return {}
