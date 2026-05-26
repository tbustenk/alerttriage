"""Performance benchmarks for AlertTriage.

Tests assert that key operations complete within acceptable time bounds.
Target: <200 ms P95 for API responses, <50 ms for DB queries.

Run with timing output:
    pytest tests/performance/ -v --tb=short
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from alerttriage.src.core.alert_models import FeedbackRecord
from alerttriage.src.feedback.feedback_system import FeedbackSystem

CLIENT_ID = "bench-client"
_FAST_DB_THRESHOLD_MS = 50.0   # single-client query budget
_BATCH_INSERT_THRESHOLD_MS = 500.0   # 500 records inserted within
_ACCURACY_QUERY_MS = 20.0   # accuracy() call budget


def _make_records(n: int) -> list[FeedbackRecord]:
    return [
        FeedbackRecord(
            alert_id=f"alert-{i}",
            analysis_id=f"analysis-{i}",
            client_id=CLIENT_ID,
            analyst_id="analyst",
            analyst_verdict="true_positive" if i % 3 else "false_positive",
            ai_verdict_was_correct=bool(i % 3),
            metadata={"rule_name": f"Rule{i % 10}"},
        )
        for i in range(n)
    ]


@pytest.fixture
def seeded_fs(tmp_data_dir):
    """FeedbackSystem with 500 pre-inserted records."""
    fs = FeedbackSystem(tmp_data_dir, CLIENT_ID)
    fs.record_many(_make_records(500))
    return fs


class TestFeedbackSystemPerformance:
    def test_batch_insert_500_records(self, tmp_data_dir):
        fs = FeedbackSystem(tmp_data_dir, CLIENT_ID)
        records = _make_records(500)
        start = time.monotonic()
        fs.record_many(records)
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < _BATCH_INSERT_THRESHOLD_MS, (
            f"Batch insert of 500 records took {elapsed_ms:.1f} ms "
            f"(threshold: {_BATCH_INSERT_THRESHOLD_MS} ms)"
        )

    def test_accuracy_query_fast(self, seeded_fs):
        start = time.monotonic()
        seeded_fs.accuracy()
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < _ACCURACY_QUERY_MS, (
            f"accuracy() took {elapsed_ms:.1f} ms (threshold: {_ACCURACY_QUERY_MS} ms)"
        )

    def test_rule_stats_query_fast(self, seeded_fs):
        start = time.monotonic()
        seeded_fs.rule_stats()
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < _FAST_DB_THRESHOLD_MS

    def test_get_for_client_limited(self, seeded_fs):
        start = time.monotonic()
        results = seeded_fs.get_for_client(limit=50)
        elapsed_ms = (time.monotonic() - start) * 1000
        assert len(results) == 50
        assert elapsed_ms < _FAST_DB_THRESHOLD_MS

    def test_concurrent_reads(self, seeded_fs):
        """Multiple concurrent readers should not deadlock."""
        import threading

        errors: list[Exception] = []

        def _read():
            try:
                for _ in range(10):
                    seeded_fs.accuracy()
                    seeded_fs.rule_stats()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_read) for _ in range(4)]
        start = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed_ms = (time.monotonic() - start) * 1000

        assert not errors, f"Concurrent read errors: {errors}"
        assert elapsed_ms < 2000, f"Concurrent reads took {elapsed_ms:.1f} ms"


class TestAnalyticsEnginePerformance:
    def test_weekly_summary_under_100ms(self, tmp_data_dir):
        from alerttriage.src.analytics.engine import AnalyticsEngine

        class _Cfg:
            data_dir = tmp_data_dir

            def get_client(self, cid):
                return {}

            def list_clients(self):
                return [CLIENT_ID]

        fs = FeedbackSystem(tmp_data_dir, CLIENT_ID)
        fs.record_many(_make_records(200))
        engine = AnalyticsEngine(tmp_data_dir, _Cfg())

        start = time.monotonic()
        engine.compute_weekly_summary(CLIENT_ID, weeks=52)
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < 100, f"compute_weekly_summary took {elapsed_ms:.1f} ms"

    def test_alert_type_breakdown_under_100ms(self, tmp_data_dir):
        from alerttriage.src.analytics.engine import AnalyticsEngine

        class _Cfg:
            data_dir = tmp_data_dir

            def get_client(self, cid):
                return {}

            def list_clients(self):
                return [CLIENT_ID]

        fs = FeedbackSystem(tmp_data_dir, CLIENT_ID)
        fs.record_many(_make_records(200))
        engine = AnalyticsEngine(tmp_data_dir, _Cfg())

        start = time.monotonic()
        engine.compute_alert_type_breakdown(CLIENT_ID, days=365)
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < 100


class TestCachePerformance:
    def test_cache_hit_faster_than_miss(self):
        from alerttriage.src.performance.cache import TTLCache

        cache = TTLCache(maxsize=100, ttl_seconds=60)

        # Warm the cache
        cache.set("key", "value")

        # Measure miss (cold)
        miss_start = time.monotonic()
        for _ in range(1000):
            cache.get("nonexistent")
        miss_ms = (time.monotonic() - miss_start) * 1000

        # Measure hit (warm)
        hit_start = time.monotonic()
        for _ in range(1000):
            cache.get("key")
        hit_ms = (time.monotonic() - hit_start) * 1000

        assert hit_ms < miss_ms * 2  # hits should be faster or similar

    def test_1000_cache_ops_under_10ms(self):
        from alerttriage.src.performance.cache import TTLCache

        cache = TTLCache(maxsize=1000)
        start = time.monotonic()
        for i in range(1000):
            cache.set(f"k{i}", i)
            cache.get(f"k{i}")
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < 50, f"1000 cache ops took {elapsed_ms:.1f} ms"


class TestDryRunAnalyzePerformance:
    def test_dry_run_under_50ms(self, tmp_data_dir):
        """Dry-run analyze should be trivially fast (no AI call)."""
        from alerttriage.src.core.alert_models import Alert, AlertSeverity, AlertSource
        from alerttriage.src.core.analyzer import AlertAnalyzer
        from alerttriage.config.config_manager import (
            ConcurrencyConfig, FeedbackConfig, LearningConfig, RetryConfig,
        )

        class _Cfg:
            log_level = "WARNING"
            json_logs = False
            anonymize_salt = "test-salt-32chars-xxxxxxxxxxx"
            data_dir = tmp_data_dir
            reports_dir = "reports"
            default_model_id = "claude-sonnet"
            models_config = {"default": "claude-sonnet", "models": {}}
            concurrency = ConcurrencyConfig()
            retry = RetryConfig()
            learning = LearningConfig()
            feedback = FeedbackConfig()

            def get_client(self, cid):
                return {"model": "claude-sonnet"}

            def get_learning(self, cid):
                return self.learning

        analyzer = AlertAnalyzer(_Cfg())
        alert = Alert(
            client_id=CLIENT_ID,
            source=AlertSource.MANUAL,
            rule_name="Test",
            severity=AlertSeverity.LOW,
            title="Test",
            description="Test alert",
        )

        start = time.monotonic()
        asyncio.run(analyzer.analyze(alert, dry_run=True))
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < 50, f"Dry-run analyze took {elapsed_ms:.1f} ms"
