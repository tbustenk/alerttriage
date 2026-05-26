"""Unit tests for FeatureFlagManager."""

from __future__ import annotations

import os

import pytest
import yaml

from alerttriage.src.config_ext.feature_flags import FeatureFlagManager

CLIENT_ID = "test-client"


class _FakeConfig:
    def get_client(self, client_id: str) -> dict:
        return {"features": {}}

    def list_clients(self) -> list[str]:
        return [CLIENT_ID]


@pytest.fixture
def flags_dir(tmp_config_dir):
    """Config dir with a minimal features.yaml."""
    features = {
        "analytics": {"enabled": True, "description": "Analytics module"},
        "webhooks": {"enabled": True, "description": "Webhook delivery"},
        "pdf_export": {"enabled": False, "description": "PDF reports"},
        "auto_dismiss": {"enabled": False, "description": "Auto-dismiss low-confidence"},
        "hot_reload": {"enabled": True, "description": "Config hot-reload"},
    }
    (tmp_config_dir / "features.yaml").write_text(yaml.safe_dump(features), encoding="utf-8")
    return tmp_config_dir


@pytest.fixture
def flags(flags_dir):
    return FeatureFlagManager(flags_dir, _FakeConfig())


class TestGlobalFlags:
    def test_get_enabled_flag(self, flags):
        assert flags.is_enabled("analytics") is True

    def test_get_disabled_flag(self, flags):
        assert flags.is_enabled("auto_dismiss") is False

    def test_list_flags_returns_all(self, flags):
        result = flags.list_flags()
        assert "analytics" in result
        assert "pdf_export" in result

    def test_unknown_flag_defaults_false(self, flags):
        assert flags.is_enabled("nonexistent_flag") is False

    def test_reload_picks_up_changes(self, flags, flags_dir):
        flags_dir_path = flags_dir / "features.yaml"
        raw = yaml.safe_load(flags_dir_path.read_text())
        raw["analytics"]["enabled"] = False
        flags_dir_path.write_text(yaml.safe_dump(raw))
        flags.reload()
        assert flags.is_enabled("analytics") is False


class TestEnvironmentOverride:
    def test_env_var_overrides_global(self, flags, monkeypatch):
        monkeypatch.setenv("ALERTTRIAGE_FEATURE_AUTO_DISMISS", "true")
        flags.reload()
        assert flags.is_enabled("auto_dismiss") is True

    def test_env_var_case_insensitive_value(self, flags, monkeypatch):
        monkeypatch.setenv("ALERTTRIAGE_FEATURE_PDF_EXPORT", "1")
        flags.reload()
        assert flags.is_enabled("pdf_export") is True

    def test_env_var_false_disables(self, flags, monkeypatch):
        monkeypatch.setenv("ALERTTRIAGE_FEATURE_ANALYTICS", "false")
        flags.reload()
        assert flags.is_enabled("analytics") is False


class TestPerClientFlags:
    def test_client_override_enables(self, flags_dir):
        class _Config:
            def get_client(self, cid: str) -> dict:
                return {"features": {"pdf_export": True}}

            def list_clients(self) -> list[str]:
                return [CLIENT_ID]

        mgr = FeatureFlagManager(flags_dir, _Config())
        assert mgr.is_enabled("pdf_export", client_id=CLIENT_ID) is True

    def test_client_override_disables(self, flags_dir):
        class _Config:
            def get_client(self, cid: str) -> dict:
                return {"features": {"analytics": False}}

            def list_clients(self) -> list[str]:
                return [CLIENT_ID]

        mgr = FeatureFlagManager(flags_dir, _Config())
        assert mgr.is_enabled("analytics", client_id=CLIENT_ID) is False

    def test_no_client_config_uses_global(self, flags):
        assert flags.is_enabled("analytics", client_id="unknown-client") is True
