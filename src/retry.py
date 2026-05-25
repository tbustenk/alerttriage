"""Async retry helper with exponential backoff and rate-limit awareness.

All backend API calls (Anthropic, OpenAI, SIEM HTTP) funnel through
:func:`with_retry` so retry policy lives in one place and is driven by
:class:`alerttriage.config.config_manager.RetryConfig`.

Two classes of exception are treated specially:

* :class:`RateLimitedError` — server told us to back off; honor ``retry_after``
  if present, otherwise fall back to exponential backoff.
* :class:`TransientBackendError` — network blip, 5xx, parse glitch; eligible
  for backoff retry.

Anything else (auth failure, validation error, ``CostLimitExceededError``)
is re-raised immediately — retrying would only burn the same error budget.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar, runtime_checkable

from alerttriage.src.logger import get_logger

log = get_logger(__name__)

T = TypeVar("T")


class RateLimitedError(Exception):
    """Backend signalled HTTP 429 / quota exhaustion.

    Attribute ``retry_after`` is seconds to wait before the next attempt;
    when ``None`` the caller falls back to exponential backoff.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TransientBackendError(Exception):
    """Failure that is plausibly retryable — network errors, 5xx, parse glitches."""


@runtime_checkable
class RetryConfigLike(Protocol):
    """Duck-typed view of ``RetryConfig`` so callers can pass any settings object.

    Anything with these five attributes (``max_attempts``, ``initial_delay_sec``,
    ``max_delay_sec``, ``factor``, ``jitter``) works — keeps ``retry.py`` free of
    a hard dependency on the config module.
    """

    max_attempts: int
    initial_delay_sec: float
    max_delay_sec: float
    factor: float
    jitter: bool


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    config: RetryConfigLike,
    op_name: str = "backend_call",
) -> T:
    """Invoke ``fn`` with exponential backoff until success or budget exhaustion.

    Args:
        fn: Zero-arg async callable. Wrap real calls in a ``lambda`` if needed.
        config: Retry knobs (max_attempts, delays, factor, jitter).
        op_name: Label used in retry log lines.

    Returns:
        Whatever ``fn`` returns on first success.

    Raises:
        The last exception raised by ``fn`` once attempts are exhausted, or
        any non-retryable exception immediately.
    """
    attempt = 0
    delay = config.initial_delay_sec
    last_exc: BaseException | None = None

    while attempt < config.max_attempts:
        attempt += 1
        try:
            return await fn()
        except RateLimitedError as exc:
            last_exc = exc
            wait = exc.retry_after if exc.retry_after is not None else delay
            wait = min(wait, config.max_delay_sec)
            log.warning(
                "retry_rate_limited",
                op=op_name,
                attempt=attempt,
                max_attempts=config.max_attempts,
                wait_sec=wait,
            )
        except TransientBackendError as exc:
            last_exc = exc
            wait = _next_delay(delay, config)
            log.warning(
                "retry_transient",
                op=op_name,
                attempt=attempt,
                max_attempts=config.max_attempts,
                wait_sec=wait,
                error=str(exc),
            )
            delay = min(delay * config.factor, config.max_delay_sec)

        if attempt >= config.max_attempts:
            break
        await asyncio.sleep(wait)

    assert last_exc is not None  # loop only exits via raise or after assigning last_exc
    raise last_exc


def _next_delay(current: float, config: RetryConfigLike) -> float:
    """Return the actual wait time, optionally jittered."""
    if not config.jitter:
        return current
    return random.uniform(current / 2.0, current)


def classify_anthropic_error(exc: BaseException) -> BaseException:
    """Map an ``anthropic`` exception to retry primitives where appropriate.

    Non-retryable errors (auth, validation, bad request) pass through unchanged.
    """
    try:
        import anthropic  # local import keeps this module light
    except ImportError:
        return exc

    if isinstance(exc, anthropic.RateLimitError):
        retry_after = _extract_retry_after(exc)
        return RateLimitedError(str(exc), retry_after=retry_after)
    if isinstance(exc, anthropic.APIConnectionError | anthropic.APITimeoutError):
        return TransientBackendError(str(exc))
    if isinstance(exc, anthropic.InternalServerError):
        return TransientBackendError(str(exc))
    return exc


def classify_openai_error(exc: BaseException) -> BaseException:
    """Map an ``openai`` exception to retry primitives where appropriate."""
    try:
        import openai
    except ImportError:
        return exc

    if isinstance(exc, openai.RateLimitError):
        retry_after = _extract_retry_after(exc)
        return RateLimitedError(str(exc), retry_after=retry_after)
    if isinstance(exc, openai.APIConnectionError | openai.APITimeoutError):
        return TransientBackendError(str(exc))
    if isinstance(exc, openai.InternalServerError):
        return TransientBackendError(str(exc))
    return exc


def _extract_retry_after(exc: BaseException) -> float | None:
    """Best-effort: pull a ``Retry-After`` header off an SDK exception."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if headers is None:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Concrete implementation of RetryConfigLike
# ---------------------------------------------------------------------------

import dataclasses


@dataclasses.dataclass
class SimpleRetryConfig:
    """Standalone RetryConfigLike for use outside the full config module.

    Satisfies the :class:`RetryConfigLike` Protocol so it can be passed
    directly to :func:`with_retry`.
    """

    max_attempts: int = 3
    initial_delay_sec: float = 0.5
    max_delay_sec: float = 30.0
    factor: float = 2.0
    jitter: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "SimpleRetryConfig":
        """Build from a config dict, ignoring unknown keys."""
        return cls(
            max_attempts=int(d.get("retry_max_attempts", cls.max_attempts)),
            initial_delay_sec=float(d.get("retry_initial_delay_sec", cls.initial_delay_sec)),
            max_delay_sec=float(d.get("retry_max_delay_sec", cls.max_delay_sec)),
            factor=float(d.get("retry_factor", cls.factor)),
            jitter=bool(d.get("retry_jitter", cls.jitter)),
        )
