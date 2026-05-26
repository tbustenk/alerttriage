"""Config versioning — snapshot, diff, and rollback for per-client configs.

Each save creates an immutable snapshot in ``data/<client_id>/config_versions/``.
The ``VERSIONS.json`` index tracks all snapshots and the current active version.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from alerttriage.src.logger import get_logger

log = get_logger(__name__)

_INDEX_FILE = "VERSIONS.json"
_MAX_VERSIONS = 20   # prune oldest beyond this


@dataclass
class ConfigVersion:
    version_id: str        # e.g. "v3_20260525_143000"
    created_at: str        # ISO timestamp
    comment: str
    is_current: bool
    snapshot_path: str     # relative to versions_dir


class ConfigVersioning:
    """Version-controlled config snapshots for one client.

    Args:
        data_dir: Root data directory (``data/``).
        config_dir: Directory that holds ``client_configs/``.
    """

    def __init__(self, data_dir: Path, config_dir: Path) -> None:
        self._data_dir = Path(data_dir)
        self._config_dir = Path(config_dir)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def snapshot(
        self,
        client_id: str,
        config: dict[str, Any],
        *,
        comment: str = "",
    ) -> str:
        """Save a snapshot and return its version_id."""
        versions_dir = self._versions_dir(client_id)
        versions_dir.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y%m%d_%H%M%S")
        index = self._load_index(client_id)
        version_num = len(index) + 1
        version_id = f"v{version_num}_{ts}"

        snapshot_path = versions_dir / f"{version_id}.yaml"
        snapshot_path.write_text(yaml.safe_dump(config, default_flow_style=False), encoding="utf-8")

        # Mark all existing as not-current, then add new
        for v in index:
            v["is_current"] = False
        index.append({
            "version_id": version_id,
            "created_at": now.isoformat(),
            "comment": comment or f"Snapshot {version_id}",
            "is_current": True,
            "snapshot_path": snapshot_path.name,
        })

        # Prune oldest if over limit
        if len(index) > _MAX_VERSIONS:
            oldest = index.pop(0)
            old_file = versions_dir / oldest["snapshot_path"]
            if old_file.exists():
                old_file.unlink()

        self._save_index(client_id, index)
        log.info("config_snapshot_created", client=client_id, version_id=version_id)
        return version_id

    def rollback(self, client_id: str, version_id: str) -> dict[str, Any]:
        """Restore a previous snapshot as the active config.

        Writes the snapshot back to the client config file and marks it current
        in the version index. Returns the loaded config dict.
        """
        versions_dir = self._versions_dir(client_id)
        index = self._load_index(client_id)

        target = next((v for v in index if v["version_id"] == version_id), None)
        if target is None:
            raise ValueError(f"Version '{version_id}' not found for client '{client_id}'")

        snapshot_path = versions_dir / target["snapshot_path"]
        if not snapshot_path.exists():
            raise FileNotFoundError(f"Snapshot file missing: {snapshot_path}")

        config = yaml.safe_load(snapshot_path.read_text(encoding="utf-8"))

        # Write back to the live client config file
        live_path = self._config_dir / "client_configs" / f"{client_id}.yaml"
        live_path.write_text(yaml.safe_dump(config, default_flow_style=False), encoding="utf-8")

        # Update index — mark this version current
        for v in index:
            v["is_current"] = v["version_id"] == version_id
        self._save_index(client_id, index)

        log.info("config_rolled_back", client=client_id, version_id=version_id)
        return config

    def history(self, client_id: str) -> list[ConfigVersion]:
        """Return all saved versions newest-first."""
        index = self._load_index(client_id)
        return [
            ConfigVersion(
                version_id=v["version_id"],
                created_at=v["created_at"],
                comment=v["comment"],
                is_current=v.get("is_current", False),
                snapshot_path=v["snapshot_path"],
            )
            for v in reversed(index)
        ]

    def get_snapshot(self, client_id: str, version_id: str) -> dict[str, Any]:
        """Load and return the config dict for a specific version."""
        versions_dir = self._versions_dir(client_id)
        index = self._load_index(client_id)
        target = next((v for v in index if v["version_id"] == version_id), None)
        if target is None:
            raise ValueError(f"Version '{version_id}' not found")
        path = versions_dir / target["snapshot_path"]
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _versions_dir(self, client_id: str) -> Path:
        return self._data_dir / client_id / "config_versions"

    def _load_index(self, client_id: str) -> list[dict[str, Any]]:
        path = self._versions_dir(client_id) / _INDEX_FILE
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    def _save_index(self, client_id: str, index: list[dict[str, Any]]) -> None:
        path = self._versions_dir(client_id) / _INDEX_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(index, indent=2), encoding="utf-8")
