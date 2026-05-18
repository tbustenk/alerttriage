"""Per-client cost tracking and budget enforcement."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from alerttriage.src.logger import get_logger

log = get_logger(__name__)


class CostLimitExceededError(Exception):
    """Raised when a client's daily or monthly budget would be exceeded."""


class CostController:
    """
    Tracks cumulative AI spend per client and blocks calls that exceed limits.

    Limits are read from the client config:
      cost_limits.daily_usd   (default: no limit)
      cost_limits.monthly_usd (default: no limit)

    Spend is persisted to `data/<client_id>/cost.json` so it survives restarts.
    """

    def __init__(self, config: Any) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._data_dir = Path(getattr(config, "data_dir", "data"))
        self._cache: dict[str, dict[str, Any]] = {}

    def check_limit(self, client_id: str) -> None:
        """Raise CostLimitExceededError if the client is over budget."""
        state = self._load(client_id)
        limits = self._get_limits(client_id)

        if limits.get("daily_usd") and state["daily_usd"] >= limits["daily_usd"]:
            raise CostLimitExceededError(
                f"Client '{client_id}' has exceeded its daily budget of "
                f"${limits['daily_usd']:.2f} (current: ${state['daily_usd']:.4f})"
            )
        if limits.get("monthly_usd") and state["monthly_usd"] >= limits["monthly_usd"]:
            raise CostLimitExceededError(
                f"Client '{client_id}' has exceeded its monthly budget of "
                f"${limits['monthly_usd']:.2f} (current: ${state['monthly_usd']:.4f})"
            )

    def record(self, client_id: str, cost_usd: float) -> None:
        """Add cost_usd to this client's running totals."""
        with self._lock:
            state = self._load(client_id)
            state["daily_usd"] += cost_usd
            state["monthly_usd"] += cost_usd
            state["total_usd"] += cost_usd
            self._save(client_id, state)
            log.debug(
                "cost_recorded",
                client=client_id,
                added=cost_usd,
                daily=state["daily_usd"],
                monthly=state["monthly_usd"],
            )

    def get_spend(self, client_id: str) -> dict[str, float]:
        state = self._load(client_id)
        return {
            "daily_usd": state["daily_usd"],
            "monthly_usd": state["monthly_usd"],
            "total_usd": state["total_usd"],
        }

    def reset_daily(self, client_id: str) -> None:
        with self._lock:
            state = self._load(client_id)
            state["daily_usd"] = 0.0
            self._save(client_id, state)

    # -----------------------------------------------------------------------
    def _path(self, client_id: str) -> Path:
        p = self._data_dir / client_id / "cost.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def _load(self, client_id: str) -> dict[str, Any]:
        if client_id in self._cache:
            return self._cache[client_id]
        p = self._path(client_id)
        if p.exists():
            state: dict[str, Any] = json.loads(p.read_text())
        else:
            state = {"daily_usd": 0.0, "monthly_usd": 0.0, "total_usd": 0.0}
        self._cache[client_id] = state
        return state

    def _save(self, client_id: str, state: dict[str, Any]) -> None:
        self._path(client_id).write_text(json.dumps(state, indent=2))
        self._cache[client_id] = state

    def _get_limits(self, client_id: str) -> dict[str, float]:
        try:
            client_cfg = self._config.get_client(client_id)
            return client_cfg.get("cost_limits", {})
        except Exception:
            return {}
