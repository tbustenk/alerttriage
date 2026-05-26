"""Unit tests for AlertTypeSettingsManager."""

from __future__ import annotations

import pytest
import yaml

from alerttriage.src.config_ext.alert_type_settings import (
    AlertTypeSettingsManager,
    _GLOBAL_DEFAULTS,
)

CLIENT_ID = "test-client"


class _FakeConfig:
    def __init__(self, client_overrides: dict | None = None):
        self._overrides = client_overrides or {}

    def get_client(self, client_id: str) -> dict:
        return {"alert_types": self._overrides.get(client_id, {})}


@pytest.fixture
def at_dir(tmp_config_dir):
    """Config dir with a minimal alert_types.yaml."""
    alert_types = {
        "_defaults": {
            "confidence_threshold": 0.75,
            "auto_dismiss": False,
        },
        "_patterns": [
            {"pattern": "brute_force_*", "confidence_threshold": 0.80, "escalate_on_low_confidence": True},
        ],
        "ransomware_detected": {
            "confidence_threshold": 0.60,
            "auto_dismiss": False,
            "escalate_on_low_confidence": True,
        },
        "port_scan": {
            "confidence_threshold": 0.85,
            "auto_dismiss": True,
        },
    }
    (tmp_config_dir / "alert_types.yaml").write_text(yaml.safe_dump(alert_types), encoding="utf-8")
    return tmp_config_dir


@pytest.fixture
def settings(at_dir):
    return AlertTypeSettingsManager(at_dir, _FakeConfig())


class TestGet:
    def test_exact_rule_overrides_defaults(self, settings):
        s = settings.get("ransomware_detected")
        assert s["confidence_threshold"] == pytest.approx(0.60)
        assert s["escalate_on_low_confidence"] is True

    def test_port_scan_auto_dismiss(self, settings):
        s = settings.get("port_scan")
        assert s["auto_dismiss"] is True

    def test_glob_pattern_applied(self, settings):
        s = settings.get("brute_force_login")
        assert s["confidence_threshold"] == pytest.approx(0.80)
        assert s["escalate_on_low_confidence"] is True

    def test_unknown_rule_uses_defaults(self, settings):
        s = settings.get("totally_unknown_rule")
        assert s["confidence_threshold"] == pytest.approx(0.75)
        assert "auto_dismiss" in s

    def test_global_defaults_always_present(self, settings):
        s = settings.get("port_scan")
        for key in _GLOBAL_DEFAULTS:
            assert key in s

    def test_client_override_applied(self, at_dir):
        mgr = AlertTypeSettingsManager(
            at_dir,
            _FakeConfig(client_overrides={CLIENT_ID: {"port_scan": {"confidence_threshold": 0.50}}}),
        )
        s = mgr.get("port_scan", client_id=CLIENT_ID)
        assert s["confidence_threshold"] == pytest.approx(0.50)


class TestListRules:
    def test_list_includes_explicit_rules(self, settings):
        rules = settings.list_rules()
        assert "ransomware_detected" in rules
        assert "port_scan" in rules

    def test_list_excludes_meta_keys(self, settings):
        rules = settings.list_rules()
        assert "_defaults" not in rules
        assert "_patterns" not in rules


class TestUpdateRule:
    def test_update_global_rule(self, settings, at_dir):
        settings.update_rule("port_scan", {"confidence_threshold": 0.99})
        at_path = at_dir / "alert_types.yaml"
        raw = yaml.safe_load(at_path.read_text())
        assert raw["port_scan"]["confidence_threshold"] == pytest.approx(0.99)

    def test_update_creates_new_rule(self, settings, at_dir):
        settings.update_rule("new_rule", {"confidence_threshold": 0.55})
        raw = yaml.safe_load((at_dir / "alert_types.yaml").read_text())
        assert "new_rule" in raw
        assert raw["new_rule"]["confidence_threshold"] == pytest.approx(0.55)

    def test_update_client_level_writes_to_client_yaml(self, at_dir, tmp_config_dir):
        # Write a client config first
        client_cfg = {"client_id": CLIENT_ID}
        cfg_path = tmp_config_dir / "client_configs" / f"{CLIENT_ID}.yaml"
        cfg_path.write_text(yaml.safe_dump(client_cfg), encoding="utf-8")

        mgr = AlertTypeSettingsManager(at_dir, _FakeConfig())
        mgr.update_rule("port_scan", {"confidence_threshold": 0.42}, client_id=CLIENT_ID)

        saved = yaml.safe_load(cfg_path.read_text())
        assert saved["alert_types"]["port_scan"]["confidence_threshold"] == pytest.approx(0.42)


class TestReload:
    def test_reload_picks_up_new_rule(self, settings, at_dir):
        path = at_dir / "alert_types.yaml"
        raw = yaml.safe_load(path.read_text())
        raw["newly_added_rule"] = {"confidence_threshold": 0.33}
        path.write_text(yaml.safe_dump(raw))
        settings.reload()
        assert "newly_added_rule" in settings.list_rules()
