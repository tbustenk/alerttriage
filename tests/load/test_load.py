"""Load tests — simulate sustained client usage patterns.

These tests verify that the system remains stable and within performance
budgets under realistic alert volumes (1000+ alerts/day per client).

Run separately to avoid slowing the main suite:
    pytest tests/load/ -v -m load
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.load

CLIENT_ID = "load-test-client"
_THROUGHPUT_TARGET = 1000   # alerts/day ≈ ~1 per 86 seconds; test burst
_BURST_SIZE = 100


def _make_alert(i: int):
    from alerttriage.src.core.alert_models import Alert, AlertSeverity, AlertSource

    return Alert(
        client_id=CLIENT_ID,
        source=AlertSource.MANUAL,
        rule_name=f"Rule{i % 10}",
        severity=AlertSeverity.MEDIUM,
        title=f"Alert {i}",
        description=f"Load test alert number {i} with moderate description text.",
    )


def _make_feedback(i: int):
    from alerttriage.src.core.alert_models import FeedbackRecord

    return FeedbackRecord(
        alert_id=f"load-alert-{i}",
        analysis_id=f"load-analysis-{i}",
        client_id=CLIENT_ID,
        analyst_id="load-tester",
        analyst_verdict="true_positive" if i % 4 else "false_positive",
        ai_verdict_was_correct=bool(i % 4),
        metadata={"rule_name": f"Rule{i % 10}"},
    )


class TestFeedbackStoreLoad:
    def test_insert_1000_records(self, tmp_data_dir):
        """1 000 feedback records must insert within 2 s."""
        from alerttriage.src.feedback.feedback_system import FeedbackSystem

        fs = FeedbackSystem(tmp_data_dir, CLIENT_ID)
        records = [_make_feedback(i) for i in range(1000)]

        start = time.monotonic()
        written = fs.record_many(records)
        elapsed = time.monotonic() - start

        assert written == 1000
        assert elapsed < 2.0, f"1 000 inserts took {elapsed:.2f} s"

    def test_accuracy_and_stats_under_load(self, tmp_data_dir):
        """Accuracy / rule_stats queries stay fast on a 1 000-record DB."""
        from alerttriage.src.feedback.feedback_system import FeedbackSystem

        fs = FeedbackSystem(tmp_data_dir, CLIENT_ID)
        fs.record_many([_make_feedback(i) for i in range(1000)])

        start = time.monotonic()
        for _ in range(50):
            fs.accuracy()
            fs.rule_stats()
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < 500, f"50 query pairs on 1k records took {elapsed_ms:.0f} ms"


class TestDryRunBurst:
    def test_burst_100_dry_run_analyses(self, tmp_data_dir):
        """100 concurrent dry-run analyses should complete within 5 seconds."""
        from alerttriage.src.core.analyzer import AlertAnalyzer
        from alerttriage.config.config_manager import (
            ConcurrencyConfig, FeedbackConfig, LearningConfig, RetryConfig,
        )

        class _Cfg:
            log_level = "WARNING"
            json_logs = False
            anonymize_salt = "load-test-salt-32chars-xxxxx"
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
        alerts = [_make_alert(i) for i in range(_BURST_SIZE)]

        start = time.monotonic()
        results = asyncio.run(analyzer.analyze_batch(alerts))
        elapsed = time.monotonic() - start

        assert len(results) == _BURST_SIZE
        assert elapsed < 5.0, f"{_BURST_SIZE} dry-run analyses took {elapsed:.2f} s"


class TestMultiClientLoad:
    def test_10_clients_concurrent_writes(self, tmp_data_dir):
        """10 clients writing concurrently must not corrupt any DB."""
        from alerttriage.src.feedback.feedback_system import FeedbackSystem

        def _write_client(client_id: str, n: int) -> int:
            fs = FeedbackSystem(tmp_data_dir, client_id)
            records = [
                _make_feedback(i)._replace(client_id=client_id, alert_id=f"{client_id}-{i}")
                if hasattr(_make_feedback(i), "_replace")
                else _make_feedback(i)
                for i in range(n)
            ]
            # Rebuild records with correct client_id
            from alerttriage.src.core.alert_models import FeedbackRecord
            proper_records = [
                FeedbackRecord(
                    alert_id=f"{client_id}-{i}",
                    analysis_id=f"analysis-{i}",
                    client_id=client_id,
                    analyst_id="tester",
                    analyst_verdict="true_positive",
                    ai_verdict_was_correct=True,
                    metadata={"rule_name": "Test"},
                )
                for i in range(n)
            ]
            return fs.record_many(proper_records)

        client_ids = [f"client-{i}" for i in range(10)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(_write_client, cid, 50) for cid in client_ids]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        # Each client wrote 50 records
        assert all(r == 50 for r in results), f"Some clients wrote wrong count: {results}"

        # Verify counts per client DB
        for cid in client_ids:
            fs = FeedbackSystem(tmp_data_dir, cid)
            assert fs.get_for_client(limit=100).__len__() == 50


class TestWebhookManagerLoad:
    def test_register_and_list_100_webhooks(self, tmp_data_dir):
        from alerttriage.src.webhooks.manager import WebhookManager

        mgr = WebhookManager(tmp_data_dir)
        start = time.monotonic()
        for i in range(100):
            mgr.register(
                client_id=f"client-{i % 10}",
                url=f"https://example.com/hook/{i}",
                events=["alert_analyzed"],
            )
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < 2000, f"Registering 100 webhooks took {elapsed_ms:.0f} ms"

        all_regs = mgr.list_all()
        assert len(all_regs) == 100

    def test_list_for_client_from_100_webhooks(self, tmp_data_dir):
        from alerttriage.src.webhooks.manager import WebhookManager

        mgr = WebhookManager(tmp_data_dir)
        for i in range(100):
            mgr.register(
                client_id="target-client" if i % 5 == 0 else f"other-{i}",
                url=f"https://example.com/hook/{i}",
                events=["alert_analyzed"],
            )

        start = time.monotonic()
        target_regs = mgr.list_for_client("target-client")
        elapsed_ms = (time.monotonic() - start) * 1000

        assert len(target_regs) == 20  # every 5th of 100
        assert elapsed_ms < 100
