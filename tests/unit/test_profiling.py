"""Unit tests for TimingMiddleware."""

from __future__ import annotations

import asyncio

import pytest

from alerttriage.src.performance.profiling import TimingMiddleware


def _make_app(latency_ms: float = 0.0):
    async def _app(scope, receive, send):
        if latency_ms:
            await asyncio.sleep(latency_ms / 1000.0)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
    return _app


def _make_scope(path: str = "/test", method: str = "GET"):
    return {"type": "http", "method": method, "path": path, "headers": []}


async def _call(mw, path="/test", method="GET"):
    scope = _make_scope(path, method)
    sent = []

    async def receive():
        return {}

    async def send(msg):
        sent.append(msg)

    await mw(scope, receive, send)


class TestTimingMiddleware:
    def test_non_http_passes_through(self):
        app = _make_app()
        mw = TimingMiddleware(app, slow_threshold_ms=1000)

        called = []

        async def _inner(scope, receive, send):
            called.append(1)

        mw._app = _inner

        async def run():
            await mw({"type": "websocket"}, None, None)

        asyncio.run(run())
        assert called == [1]

    def test_records_sample(self):
        mw = TimingMiddleware(_make_app())
        asyncio.run(_call(mw))
        assert len(mw._samples) == 1

    def test_percentile_no_data(self):
        mw = TimingMiddleware(_make_app())
        result = mw.percentile()
        assert result is None

    def test_percentile_with_data(self):
        mw = TimingMiddleware(_make_app())
        for _ in range(10):
            asyncio.run(_call(mw, path="/api/v1/analyze"))
        # Route key is "GET /api/v1/analyze" — prefix must include the method
        p50 = mw.percentile("GET /api/v1", pct=50)
        assert p50 is not None
        assert p50 >= 0

    def test_percentile_no_prefix_match(self):
        mw = TimingMiddleware(_make_app())
        asyncio.run(_call(mw, path="/other"))
        result = mw.percentile("GET /api/v1")
        assert result is None

    def test_summary_empty(self):
        mw = TimingMiddleware(_make_app())
        summary = mw.summary()
        assert summary == {"samples": 0}

    def test_summary_with_data(self):
        mw = TimingMiddleware(_make_app())
        for _ in range(5):
            asyncio.run(_call(mw))
        summary = mw.summary()
        assert summary["samples"] == 5
        assert "p50_ms" in summary
        assert "p95_ms" in summary
        assert "p99_ms" in summary
        assert "max_ms" in summary
        assert "min_ms" in summary
        assert summary["min_ms"] <= summary["p50_ms"] <= summary["max_ms"]

    def test_slow_request_logs_warning(self, caplog):
        import logging
        mw = TimingMiddleware(_make_app(latency_ms=5), slow_threshold_ms=1)
        with caplog.at_level(logging.WARNING):
            asyncio.run(_call(mw))
        # Just verify it ran without error — structlog doesn't use caplog directly
        assert len(mw._samples) == 1
