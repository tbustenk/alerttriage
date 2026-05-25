"""Unit tests for the shared retry helper."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from alerttriage.src.retry import (
    RateLimitedError,
    TransientBackendError,
    with_retry,
)


@dataclass
class _Cfg:
    max_attempts: int = 3
    initial_delay_sec: float = 0.001
    max_delay_sec: float = 0.01
    factor: float = 2.0
    jitter: bool = False


def test_returns_first_success() -> None:
    calls = {"n": 0}

    async def fn() -> str:
        calls["n"] += 1
        return "ok"

    result = asyncio.run(with_retry(fn, config=_Cfg()))
    assert result == "ok"
    assert calls["n"] == 1


def test_retries_on_transient_then_succeeds() -> None:
    calls = {"n": 0}

    async def fn() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientBackendError("boom")
        return "ok"

    result = asyncio.run(with_retry(fn, config=_Cfg(max_attempts=3)))
    assert result == "ok"
    assert calls["n"] == 3


def test_raises_after_exhausting_attempts() -> None:
    async def fn() -> str:
        raise TransientBackendError("permanent")

    with pytest.raises(TransientBackendError):
        asyncio.run(with_retry(fn, config=_Cfg(max_attempts=2)))


def test_rate_limited_honors_retry_after_cap() -> None:
    calls = {"n": 0}

    async def fn() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise RateLimitedError("slow down", retry_after=999.0)
        return "ok"

    # max_delay_sec caps the 999s suggestion to 0.01s for the test.
    result = asyncio.run(with_retry(fn, config=_Cfg(max_attempts=2, max_delay_sec=0.01)))
    assert result == "ok"


def test_non_retryable_exception_propagates_immediately() -> None:
    calls = {"n": 0}

    class AuthError(Exception):
        pass

    async def fn() -> str:
        calls["n"] += 1
        raise AuthError("invalid key")

    with pytest.raises(AuthError):
        asyncio.run(with_retry(fn, config=_Cfg(max_attempts=5)))
    assert calls["n"] == 1
