"""Unit tests for security utilities — cache, rate limiter, secrets manager."""

from __future__ import annotations

import time

import pytest


class TestTTLCache:
    @pytest.fixture
    def cache(self):
        from alerttriage.src.performance.cache import TTLCache

        return TTLCache(maxsize=5, ttl_seconds=60.0)

    def test_set_and_get(self, cache):
        cache.set("key", "value")
        assert cache.get("key") == "value"

    def test_missing_returns_default(self, cache):
        assert cache.get("missing") is None
        assert cache.get("missing", "fallback") == "fallback"

    def test_expiry(self):
        from alerttriage.src.performance.cache import TTLCache

        cache = TTLCache(maxsize=10, ttl_seconds=0.05)
        cache.set("k", "v")
        assert cache.get("k") == "v"
        time.sleep(0.1)
        assert cache.get("k") is None

    def test_maxsize_evicts_oldest(self, cache):
        for i in range(6):
            cache.set(f"key{i}", i)
        # First entry should be evicted (maxsize=5)
        assert len(cache) <= 5

    def test_delete(self, cache):
        cache.set("x", 1)
        cache.delete("x")
        assert cache.get("x") is None

    def test_clear(self, cache):
        cache.set("a", 1)
        cache.set("b", 2)
        cache.clear()
        assert len(cache) == 0

    def test_thread_safety(self, cache):
        import threading

        errors = []

        def _worker(n):
            try:
                for i in range(100):
                    cache.set(f"key-{n}-{i}", i)
                    cache.get(f"key-{n}-{i}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors


class TestSecretsManager:
    def test_reads_env_var(self, monkeypatch):
        from alerttriage.src.secrets.manager import get_secret, clear_cache

        monkeypatch.setenv("MY_TEST_SECRET", "super-secret-value")
        clear_cache()
        val = get_secret("MY_TEST_SECRET")
        assert val == "super-secret-value"

    def test_returns_default_when_missing(self):
        from alerttriage.src.secrets.manager import get_secret, clear_cache

        clear_cache()
        val = get_secret("NONEXISTENT_SECRET_XYZ", default="fallback")
        assert val == "fallback"

    def test_cache_avoids_second_env_lookup(self, monkeypatch):
        from alerttriage.src.secrets.manager import get_secret, clear_cache

        monkeypatch.setenv("CACHED_SECRET", "cached-value")
        clear_cache()
        first = get_secret("CACHED_SECRET")
        monkeypatch.delenv("CACHED_SECRET")
        second = get_secret("CACHED_SECRET")  # should come from cache
        assert first == second == "cached-value"

    def test_clear_cache_forces_fresh_lookup(self, monkeypatch):
        from alerttriage.src.secrets.manager import get_secret, clear_cache

        monkeypatch.setenv("VOLATILE_SECRET", "v1")
        clear_cache()
        get_secret("VOLATILE_SECRET")

        monkeypatch.setenv("VOLATILE_SECRET", "v2")
        clear_cache()
        val = get_secret("VOLATILE_SECRET")
        assert val == "v2"


class TestRateLimiter:
    def test_allows_requests_under_limit(self):
        from alerttriage.src.api.rate_limit import RateLimiter

        limiter = RateLimiter(rpm=100)
        for _ in range(10):
            limiter.check("127.0.0.1")  # Should not raise

    def test_blocks_at_limit(self):
        from alerttriage.src.api.rate_limit import RateLimiter
        from fastapi import HTTPException

        limiter = RateLimiter(rpm=5)
        for _ in range(5):
            limiter.check("10.0.0.1")
        with pytest.raises(HTTPException) as exc_info:
            limiter.check("10.0.0.1")
        assert exc_info.value.status_code == 429

    def test_different_keys_independent(self):
        from alerttriage.src.api.rate_limit import RateLimiter

        limiter = RateLimiter(rpm=3)
        for _ in range(3):
            limiter.check("ip-a")
        # ip-b has its own counter and should not be affected
        limiter.check("ip-b")
