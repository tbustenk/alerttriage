"""Unit tests for the retry module."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alerttriage.src.retry import (
    SimpleRetryConfig as RetryConfig,
    RateLimitedError,
    TransientBackendError,
    _next_delay,
    with_retry as retry_with_backoff,
    classify_anthropic_error,
)


class TestRetryWithBackoff:
    def test_success_on_first_try(self):
        called = []

        async def _fn():
            called.append(1)
            return "ok"

        result = asyncio.run(retry_with_backoff(_fn, config=RetryConfig(max_attempts=3), op_name="test"))
        assert result == "ok"
        assert len(called) == 1

    def test_retries_on_transient(self):
        calls = []

        async def _fn():
            calls.append(1)
            if len(calls) < 3:
                raise TransientBackendError("temp")
            return "done"

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = asyncio.run(
                retry_with_backoff(_fn, config=RetryConfig(max_attempts=3, initial_delay_sec=0.01), op_name="op")
            )
        assert result == "done"
        assert len(calls) == 3

    def test_exhausts_transient_raises(self):
        async def _fn():
            raise TransientBackendError("always fails")

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(TransientBackendError):
                asyncio.run(
                    retry_with_backoff(_fn, config=RetryConfig(max_attempts=2, initial_delay_sec=0.01), op_name="op")
                )

    def test_retries_on_rate_limit(self):
        calls = []

        async def _fn():
            calls.append(1)
            if len(calls) < 2:
                raise RateLimitedError("rate limited", retry_after=0.001)
            return "ok"

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = asyncio.run(
                retry_with_backoff(_fn, config=RetryConfig(max_attempts=3, initial_delay_sec=0.01), op_name="ratelimit-op")
            )
        assert result == "ok"
        assert len(calls) == 2

    def test_exhausts_rate_limit_raises(self):
        async def _fn():
            raise RateLimitedError("rate limited")

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(RateLimitedError):
                asyncio.run(
                    retry_with_backoff(_fn, config=RetryConfig(max_attempts=2, initial_delay_sec=0.01), op_name="op")
                )

    def test_non_retryable_raises_immediately(self):
        calls = []

        async def _fn():
            calls.append(1)
            raise ValueError("not retryable")

        with pytest.raises(ValueError):
            asyncio.run(
                retry_with_backoff(_fn, config=RetryConfig(max_attempts=3), op_name="op")
            )
        assert len(calls) == 1


class TestNextDelay:
    def test_no_jitter(self):
        config = RetryConfig(jitter=False)
        result = _next_delay(1.5, config)
        assert result == 1.5

    def test_with_jitter(self):
        config = RetryConfig(jitter=True)
        result = _next_delay(2.0, config)
        assert 1.0 <= result <= 2.0


class TestClassifyErrors:
    def test_classify_non_anthropic(self):
        exc = ValueError("plain error")
        result = classify_anthropic_error(exc)
        assert result is exc

    def test_classify_no_anthropic_installed(self):
        with patch.dict("sys.modules", {"anthropic": None}):
            exc = RuntimeError("test")
            result = classify_anthropic_error(exc)
            assert result is exc

    def test_classify_anthropic_rate_limit(self):
        mock_exc = Exception("rate limited")
        mock_anthropic = MagicMock()
        mock_anthropic.RateLimitError = type(mock_exc)
        mock_anthropic.APIConnectionError = type(None)
        mock_anthropic.APITimeoutError = type(None)
        mock_anthropic.InternalServerError = type(None)

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            result = classify_anthropic_error(mock_exc)
        assert isinstance(result, RateLimitedError)

    def test_classify_anthropic_connection_error(self):
        mock_exc = Exception("connection failed")
        mock_anthropic = MagicMock()
        mock_anthropic.RateLimitError = type(None)
        mock_anthropic.APIConnectionError = type(mock_exc)
        mock_anthropic.APITimeoutError = type(None)
        mock_anthropic.InternalServerError = type(None)

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            result = classify_anthropic_error(mock_exc)
        assert isinstance(result, TransientBackendError)

    def test_classify_anthropic_internal_server(self):
        mock_exc = Exception("server error")
        mock_anthropic = MagicMock()
        mock_anthropic.RateLimitError = type(None)
        mock_anthropic.APIConnectionError = type(None)
        mock_anthropic.APITimeoutError = type(None)
        mock_anthropic.InternalServerError = type(mock_exc)

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            result = classify_anthropic_error(mock_exc)
        assert isinstance(result, TransientBackendError)

    def test_classify_openai_error_no_openai(self):
        from alerttriage.src.retry import classify_openai_error
        with patch.dict("sys.modules", {"openai": None}):
            exc = ValueError("test")
            result = classify_openai_error(exc)
            assert result is exc

    def test_classify_openai_rate_limit(self):
        from alerttriage.src.retry import classify_openai_error
        mock_exc = Exception("openai rate limit")
        mock_openai = MagicMock()
        mock_openai.RateLimitError = type(mock_exc)
        mock_openai.APIConnectionError = type(None)
        mock_openai.APITimeoutError = type(None)
        mock_openai.InternalServerError = type(None)

        with patch.dict("sys.modules", {"openai": mock_openai}):
            result = classify_openai_error(mock_exc)
        assert isinstance(result, RateLimitedError)

    def test_classify_openai_connection(self):
        from alerttriage.src.retry import classify_openai_error
        mock_exc = Exception("connect error")
        mock_openai = MagicMock()
        mock_openai.RateLimitError = type(None)
        mock_openai.APIConnectionError = type(mock_exc)
        mock_openai.APITimeoutError = type(None)
        mock_openai.InternalServerError = type(None)

        with patch.dict("sys.modules", {"openai": mock_openai}):
            result = classify_openai_error(mock_exc)
        assert isinstance(result, TransientBackendError)


class TestExtractRetryAfter:
    def test_no_response(self):
        from alerttriage.src.retry import _extract_retry_after
        exc = ValueError("no response")
        assert _extract_retry_after(exc) is None

    def test_no_headers(self):
        from alerttriage.src.retry import _extract_retry_after
        exc = MagicMock()
        exc.response = MagicMock()
        exc.response.headers = None
        assert _extract_retry_after(exc) is None

    def test_retry_after_numeric(self):
        from alerttriage.src.retry import _extract_retry_after
        exc = MagicMock()
        exc.response.headers = {"retry-after": "30"}
        result = _extract_retry_after(exc)
        assert result == 30.0

    def test_retry_after_capitalized(self):
        from alerttriage.src.retry import _extract_retry_after
        exc = MagicMock()
        exc.response.headers = {"Retry-After": "60", "retry-after": None}
        result = _extract_retry_after(exc)
        assert result == 60.0

    def test_retry_after_invalid(self):
        from alerttriage.src.retry import _extract_retry_after
        exc = MagicMock()
        exc.response.headers = {"retry-after": "not-a-number"}
        result = _extract_retry_after(exc)
        assert result is None


class TestSimpleRetryConfigFromDict:
    def test_from_dict_defaults(self):
        config = RetryConfig.from_dict({})
        assert config.max_attempts == 3
        assert config.jitter is True

    def test_from_dict_custom(self):
        config = RetryConfig.from_dict({
            "retry_max_attempts": 5,
            "retry_initial_delay_sec": 1.0,
            "retry_jitter": False,
        })
        assert config.max_attempts == 5
        assert config.initial_delay_sec == 1.0
        assert config.jitter is False
