"""Per-client cost tracking and budget enforcement.

Spend is persisted to ``data/<client_id>/cost.json`` so it survives
restarts. The file is written atomically (``write + rename``) to avoid
half-written state if the process dies mid-write.

Daily and monthly totals reset automatically when the wall-clock day or
month changes since the last update — callers don't need a cron job.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

from alerttriage.src.logger import get_logger

log = get_logger(__name__)


class CostLimitExceededError(Exception):
    """Raised when a client's daily or monthly budget would be exceeded."""


class CostController:
    """Tracks cumulative AI spend per client and blocks calls that exceed limits.

    Limits are read from the client config:

      cost_limits.daily_usd   (``None`` = no limit)
      cost_limits.monthly_usd (``None`` = no limit)
    """

    def __init__(self, config: Any) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._data_dir = Path(getattr(config, "data_dir", "data"))
        self._cache: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_limit(self, client_id: str) -> None:
        """Raise :class:`CostLimitExceededError` if the client is over budget."""
        state = self._load(client_id)
        limits = self._get_limits(client_id)

        daily_cap = limits.get("daily_usd")
        if daily_cap is not None and state["daily_usd"] >= daily_cap:
            raise CostLimitExceededError(
                f"Client '{client_id}' has exceeded its daily budget of "
                f"${daily_cap:.2f} (current: ${state['daily_usd']:.4f})"
            )
        monthly_cap = limits.get("monthly_usd")
        if monthly_cap is not None and state["monthly_usd"] >= monthly_cap:
            raise CostLimitExceededError(
                f"Client '{client_id}' has exceeded its monthly budget of "
                f"${monthly_cap:.2f} (current: ${state['monthly_usd']:.4f})"
            )

    def record(self, client_id: str, cost_usd: float) -> None:
        """Add ``cost_usd`` to this client's running totals."""
        if cost_usd <= 0:
            return
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
        """Return ``{daily_usd, monthly_usd, total_usd}`` for the client."""
        state = self._load(client_id)
        return {
            "daily_usd": state["daily_usd"],
            "monthly_usd": state["monthly_usd"],
            "total_usd": state["total_usd"],
        }

    def reset_daily(self, client_id: str) -> None:
        """Force the daily counter back to zero (for ops use)."""
        with self._lock:
            state = self._load(client_id)
            state["daily_usd"] = 0.0
            state["daily_anchor"] = date.today().isoformat()
            self._save(client_id, state)

    def reset_monthly(self, client_id: str) -> None:
        """Force the monthly counter back to zero (for ops use)."""
        with self._lock:
            state = self._load(client_id)
            state["monthly_usd"] = 0.0
            state["monthly_anchor"] = _month_anchor(date.today())
            self._save(client_id, state)

    # ------------------------------------------------------------------
    # Internal — paths, IO, rollover
    # ------------------------------------------------------------------

    def _path(self, client_id: str) -> Path:
        p = self._data_dir / client_id / "cost.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def _load(self, client_id: str) -> dict[str, Any]:
        if client_id in self._cache:
            state = self._cache[client_id]
        else:
            state = self._read_from_disk(client_id)
            self._cache[client_id] = state
        self._rollover_in_place(state)
        return state

    def _read_from_disk(self, client_id: str) -> dict[str, Any]:
        path = self._path(client_id)
        if not path.exists():
            return _empty_state()
        try:
            raw = path.read_text(encoding="utf-8")
            data: dict[str, Any] = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            log.error(
                "cost_state_corrupt",
                client=client_id,
                path=str(path),
                error=str(exc),
            )
            return _empty_state()
        # Backfill new keys for files written by older versions.
        for key, default in _empty_state().items():
            data.setdefault(key, default)
        return data

    def _save(self, client_id: str, state: dict[str, Any]) -> None:
        """Atomically write state by writing to a temp file then renaming."""
        path = self._path(client_id)
        tmp_fd, tmp_path = tempfile.mkstemp(prefix=".cost-", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2)
            os.replace(tmp_path, path)
        except OSError:
            # Best-effort cleanup of the temp file on failure.
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
        self._cache[client_id] = state

    @staticmethod
    def _rollover_in_place(state: dict[str, Any]) -> None:
        """Reset daily/monthly counters when the calendar has moved on."""
        today = date.today()
        today_iso = today.isoformat()
        if state.get("daily_anchor") != today_iso:
            state["daily_usd"] = 0.0
            state["daily_anchor"] = today_iso

        anchor = _month_anchor(today)
        if state.get("monthly_anchor") != anchor:
            state["monthly_usd"] = 0.0
            state["monthly_anchor"] = anchor

    def _get_limits(self, client_id: str) -> dict[str, float | None]:
        try:
            client_cfg = self._config.get_client(client_id)
        except Exception as exc:  # noqa: BLE001 — never block analysis on config lookup
            log.warning("cost_limit_lookup_failed", client=client_id, error=str(exc))
            return {}
        return dict(client_cfg.get("cost_limits") or {})


def _empty_state() -> dict[str, Any]:
    today = date.today()
    return {
        "daily_usd": 0.0,
        "monthly_usd": 0.0,
        "total_usd": 0.0,
        "daily_anchor": today.isoformat(),
        "monthly_anchor": _month_anchor(today),
        "first_seen": datetime.utcnow().isoformat(),
    }


def _month_anchor(d: date) -> str:
    """Return ``YYYY-MM`` for the date — used as the monthly rollover key."""
    return f"{d.year:04d}-{d.month:02d}"
