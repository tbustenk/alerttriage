"""Integration tests for config versioning, feature flags, and alert-type settings."""

from __future__ import annotations

import pytest
import yaml

from alerttriage.src.config_ext.versioning import ConfigVersioning
from alerttriage.src.config_ext.feature_flags import FeatureFlagManager
from alerttriage.src.config_ext.alert_type_settings import AlertTypeSettingsManager

CLIENT_ID = "test-client"


class _FakeConfig:
    def get_client(self, cid: str) -> dict:
        return {"features": {}, "alert_types": {}}

    def list_clients(self) -> list[str]:
        return [CLIENT_ID]


# ---------------------------------------------------------------------------
# Config versioning flow
# ---------------------------------------------------------------------------


class TestVersioningFlow:
    def test_snapshot_then_rollback_loop(self, tmp_data_dir, tmp_config_dir):
        """Snapshot v1 → modify config → snapshot v2 → rollback to v1."""
        versioning = ConfigVersioning(tmp_data_dir, tmp_config_dir)

        # Create v1
        cfg_v1 = {"client_id": CLIENT_ID, "cost_limit_usd": 10.0}
        cfg_path = tmp_config_dir / "client_configs" / f"{CLIENT_ID}.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg_v1), encoding="utf-8")
        v1_id = versioning.snapshot(CLIENT_ID, cfg_v1, comment="initial")

        # Modify to v2
        cfg_v2 = {**cfg_v1, "cost_limit_usd": 99.0}
        cfg_path.write_text(yaml.safe_dump(cfg_v2), encoding="utf-8")
        versioning.snapshot(CLIENT_ID, cfg_v2, comment="raised limit")

        # Verify history has 2 entries
        history = versioning.history(CLIENT_ID)
        assert len(history) == 2

        # Roll back to v1
        versioning.rollback(CLIENT_ID, v1_id)
        restored = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert restored["cost_limit_usd"] == pytest.approx(10.0)

    def test_snapshot_before_each_update(self, tmp_data_dir, tmp_config_dir):
        """Simulates the API behaviour: auto-snapshot before every update."""
        versioning = ConfigVersioning(tmp_data_dir, tmp_config_dir)
        cfg_path = tmp_config_dir / "client_configs" / f"{CLIENT_ID}.yaml"

        configs = [{"client_id": CLIENT_ID, "version": i} for i in range(5)]
        for cfg in configs:
            if cfg_path.exists():
                current = yaml.safe_load(cfg_path.read_text())
                versioning.snapshot(CLIENT_ID, current, comment="auto")
            cfg_path.write_text(yaml.safe_dump(cfg))

        assert len(versioning.history(CLIENT_ID)) == 4  # 4 snapshots (before each of 4 updates)


# ---------------------------------------------------------------------------
# Feature flags + alert types interaction
# ---------------------------------------------------------------------------


class TestFeatureFlagsAndAlertTypes:
    @pytest.fixture
    def full_config_dir(self, tmp_config_dir):
        features = {
            "analytics": {"enabled": True},
            "pdf_export": {"enabled": False},
        }
        alert_types = {
            "_defaults": {"confidence_threshold": 0.70},
            "port_scan": {"confidence_threshold": 0.85, "auto_dismiss": True},
        }
        (tmp_config_dir / "features.yaml").write_text(yaml.safe_dump(features))
        (tmp_config_dir / "alert_types.yaml").write_text(yaml.safe_dump(alert_types))
        return tmp_config_dir

    def test_feature_flag_and_settings_independent(self, full_config_dir):
        flags = FeatureFlagManager(full_config_dir, _FakeConfig())
        settings = AlertTypeSettingsManager(full_config_dir, _FakeConfig())

        assert flags.is_enabled("analytics") is True
        assert flags.is_enabled("pdf_export") is False
        ps = settings.get("port_scan")
        assert ps["auto_dismiss"] is True
        assert ps["confidence_threshold"] == pytest.approx(0.85)

    def test_reload_both_on_change(self, full_config_dir):
        flags = FeatureFlagManager(full_config_dir, _FakeConfig())
        settings = AlertTypeSettingsManager(full_config_dir, _FakeConfig())

        # Modify both files
        feat_path = full_config_dir / "features.yaml"
        raw = yaml.safe_load(feat_path.read_text())
        raw["pdf_export"]["enabled"] = True
        feat_path.write_text(yaml.safe_dump(raw))

        at_path = full_config_dir / "alert_types.yaml"
        raw2 = yaml.safe_load(at_path.read_text())
        raw2["port_scan"]["confidence_threshold"] = 0.50
        at_path.write_text(yaml.safe_dump(raw2))

        flags.reload()
        settings.reload()

        assert flags.is_enabled("pdf_export") is True
        assert settings.get("port_scan")["confidence_threshold"] == pytest.approx(0.50)
