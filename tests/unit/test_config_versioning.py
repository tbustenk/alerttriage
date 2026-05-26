"""Unit tests for ConfigVersioning."""

from __future__ import annotations

import pytest
import yaml

from alerttriage.src.config_ext.versioning import ConfigVersioning

CLIENT_ID = "test-client"

SAMPLE_CONFIG: dict = {
    "client_id": CLIENT_ID,
    "model": "claude-sonnet",
    "cost_limit_usd": 50.0,
}


@pytest.fixture
def versioning(tmp_data_dir, tmp_config_dir):
    return ConfigVersioning(tmp_data_dir, tmp_config_dir)


@pytest.fixture
def config_with_yaml(tmp_config_dir):
    """Write a starter YAML so rollback has a target file."""
    p = tmp_config_dir / "client_configs" / f"{CLIENT_ID}.yaml"
    p.write_text(yaml.safe_dump(SAMPLE_CONFIG), encoding="utf-8")
    return tmp_config_dir


class TestSnapshot:
    def test_snapshot_returns_version_id(self, versioning):
        vid = versioning.snapshot(CLIENT_ID, SAMPLE_CONFIG, comment="initial")
        assert vid.startswith("v1_")

    def test_snapshot_increments_version(self, versioning):
        v1 = versioning.snapshot(CLIENT_ID, SAMPLE_CONFIG, comment="first")
        v2 = versioning.snapshot(CLIENT_ID, SAMPLE_CONFIG, comment="second")
        assert v1 != v2
        assert v2.startswith("v2_")

    def test_snapshot_stores_comment(self, versioning):
        versioning.snapshot(CLIENT_ID, SAMPLE_CONFIG, comment="my note")
        history = versioning.history(CLIENT_ID)
        assert history[0].comment == "my note"

    def test_snapshot_creates_yaml_file(self, tmp_data_dir, versioning):
        versioning.snapshot(CLIENT_ID, SAMPLE_CONFIG)
        versions_dir = tmp_data_dir / CLIENT_ID / "config_versions"
        yaml_files = list(versions_dir.glob("v1_*.yaml"))
        assert len(yaml_files) == 1


class TestHistory:
    def test_history_empty_returns_empty_list(self, versioning):
        assert versioning.history(CLIENT_ID) == []

    def test_history_newest_first(self, versioning):
        versioning.snapshot(CLIENT_ID, SAMPLE_CONFIG, comment="v1")
        versioning.snapshot(CLIENT_ID, SAMPLE_CONFIG, comment="v2")
        versioning.snapshot(CLIENT_ID, SAMPLE_CONFIG, comment="v3")
        history = versioning.history(CLIENT_ID)
        assert len(history) == 3
        assert history[0].comment == "v3"
        assert history[-1].comment == "v1"

    def test_history_contains_version_ids(self, versioning):
        versioning.snapshot(CLIENT_ID, SAMPLE_CONFIG)
        history = versioning.history(CLIENT_ID)
        assert len(history) == 1
        assert history[0].version_id.startswith("v1_")


class TestGetSnapshot:
    def test_get_existing_snapshot(self, versioning):
        vid = versioning.snapshot(CLIENT_ID, SAMPLE_CONFIG, comment="snap")
        data = versioning.get_snapshot(CLIENT_ID, vid)
        assert data["client_id"] == CLIENT_ID

    def test_get_missing_snapshot_raises(self, versioning):
        with pytest.raises((ValueError, FileNotFoundError)):
            versioning.get_snapshot(CLIENT_ID, "v99_20260101_000000")


class TestRollback:
    def test_rollback_restores_config(self, versioning, tmp_config_dir):
        old_cfg = {**SAMPLE_CONFIG, "cost_limit_usd": 10.0}
        vid = versioning.snapshot(CLIENT_ID, old_cfg, comment="old")

        # Write a newer config to the target YAML
        cfg_path = tmp_config_dir / "client_configs" / f"{CLIENT_ID}.yaml"
        cfg_path.write_text(yaml.safe_dump({**SAMPLE_CONFIG, "cost_limit_usd": 99.0}), encoding="utf-8")

        versioning.rollback(CLIENT_ID, vid)
        restored = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert restored["cost_limit_usd"] == pytest.approx(10.0)

    def test_rollback_nonexistent_raises(self, versioning):
        with pytest.raises((ValueError, FileNotFoundError)):
            versioning.rollback(CLIENT_ID, "ghost-version")


class TestPruning:
    def test_max_versions_pruned(self, versioning):
        """Older snapshots are pruned once _MAX_VERSIONS is exceeded."""
        from alerttriage.src.config_ext.versioning import _MAX_VERSIONS

        for i in range(_MAX_VERSIONS + 5):
            versioning.snapshot(CLIENT_ID, {**SAMPLE_CONFIG, "idx": i})

        history = versioning.history(CLIENT_ID)
        assert len(history) <= _MAX_VERSIONS
