"""Unit tests for CostController."""

from __future__ import annotations

import json
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from alerttriage.src.cost_controller import CostController, CostLimitExceededError


class _Cfg:
    def __init__(
        self, data_dir: str, *, daily: float | None = None, monthly: float | None = None
    ) -> None:
        self.data_dir = data_dir
        self._limits = {"daily_usd": daily, "monthly_usd": monthly}

    def get_client(self, client_id: str) -> dict:
        return {"client_id": client_id, "cost_limits": self._limits}


def test_record_accumulates() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cc = CostController(_Cfg(tmp))
        cc.record("acme", 0.10)
        cc.record("acme", 0.05)
        spend = cc.get_spend("acme")
        assert spend["daily_usd"] == pytest.approx(0.15)
        assert spend["monthly_usd"] == pytest.approx(0.15)
        assert spend["total_usd"] == pytest.approx(0.15)


def test_daily_limit_blocks_further_calls() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cc = CostController(_Cfg(tmp, daily=1.00))
        cc.record("acme", 0.99)
        cc.check_limit("acme")  # still under
        cc.record("acme", 0.02)
        with pytest.raises(CostLimitExceededError):
            cc.check_limit("acme")


def test_monthly_limit_blocks_further_calls() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cc = CostController(_Cfg(tmp, monthly=5.00))
        cc.record("acme", 5.00)
        with pytest.raises(CostLimitExceededError):
            cc.check_limit("acme")


def test_no_limits_means_no_blocking() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cc = CostController(_Cfg(tmp))
        cc.record("acme", 1_000_000.0)
        cc.check_limit("acme")  # never raises


def test_daily_rollover_resets_counter() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "acme" / "cost.json"
        path.parent.mkdir(parents=True)
        # Simulate state written yesterday.
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        path.write_text(
            json.dumps(
                {
                    "daily_usd": 9.99,
                    "monthly_usd": 9.99,
                    "total_usd": 9.99,
                    "daily_anchor": yesterday,
                    "monthly_anchor": yesterday[:7],
                    "first_seen": yesterday,
                }
            )
        )
        cc = CostController(_Cfg(tmp, daily=1.00))
        spend = cc.get_spend("acme")
        assert spend["daily_usd"] == 0.0  # rolled over
        assert spend["total_usd"] == 9.99  # cumulative is preserved


def test_recovers_from_corrupt_cost_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "acme" / "cost.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not valid json")
        cc = CostController(_Cfg(tmp))
        assert cc.get_spend("acme") == {"daily_usd": 0.0, "monthly_usd": 0.0, "total_usd": 0.0}


def test_record_writes_atomically() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cc = CostController(_Cfg(tmp))
        cc.record("acme", 0.42)
        # File should exist and be valid JSON after the write.
        data = json.loads((Path(tmp) / "acme" / "cost.json").read_text())
        assert data["total_usd"] == pytest.approx(0.42)
        # No leftover temp files (the .tmp suffix should have been renamed).
        assert not list((Path(tmp) / "acme").glob(".cost-*.tmp"))


def test_record_ignores_non_positive_cost() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cc = CostController(_Cfg(tmp))
        cc.record("acme", 0.0)
        cc.record("acme", -0.5)
        assert cc.get_spend("acme")["total_usd"] == 0.0
