"""Compose system prompts that include client-specific feedback guidance.

``PromptEnhancer`` is the single touchpoint between the backends and the
feedback subsystem. Backends ask for the system prompt they should send to
the AI; the enhancer either returns the base prompt unchanged or prepends
the cached learning hints derived from analyst feedback.

The hints are cached by ``client_id`` with a TTL (see
:class:`alerttriage.config.config_manager.LearningConfig`) so the hot path
never blocks on a SQLite scan.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from alerttriage.src.feedback.learning_engine import LearningEngine
from alerttriage.src.logger import get_logger

log = get_logger(__name__)

LearningEngineFactory = Callable[[str], LearningEngine]


@dataclass(frozen=True)
class _CacheEntry:
    """One memoised hints block plus its expiry timestamp."""

    hints: str
    expires_at: float


class PromptEnhancer:
    """Prepends learning-derived hints to a base system prompt.

    One instance is intended to live for the whole process and to be shared
    across backends; the cache is keyed by ``client_id``.
    """

    def __init__(
        self,
        learning_engine_factory: LearningEngineFactory,
        *,
        cache_ttl_sec: int = 300,
        max_hints: int = 10,
    ) -> None:
        """Construct an enhancer.

        Args:
            learning_engine_factory: Callable returning a :class:`LearningEngine`
                for a given ``client_id``. A factory (rather than a single
                engine) lets each client keep its own SQLite handle.
            cache_ttl_sec: How long compiled hints stay fresh. ``0`` disables caching.
            max_hints: Cap on hints prepended per call.
        """
        self._factory = learning_engine_factory
        self._ttl = cache_ttl_sec
        self._max_hints = max_hints
        self._cache: dict[str, _CacheEntry] = {}

    def enhance(self, base_prompt: str, *, client_id: str) -> str:
        """Return ``base_prompt`` with cached learning hints prepended.

        If no hints are available (no feedback yet, store unreadable, etc.)
        the base prompt is returned unchanged — analysis must keep working.
        """
        hints = self._get_hints(client_id)
        if not hints:
            return base_prompt
        return f"{hints}\n\n{base_prompt}"

    def invalidate(self, client_id: str | None = None) -> None:
        """Drop cached hints for one client, or all clients when ``None``."""
        if client_id is None:
            self._cache.clear()
        else:
            self._cache.pop(client_id, None)

    # ------------------------------------------------------------------

    def _get_hints(self, client_id: str) -> str:
        now = time.monotonic()
        entry = self._cache.get(client_id)
        if entry is not None and entry.expires_at > now:
            return entry.hints

        try:
            engine = self._factory(client_id)
            hints = engine.generate_prompt_hints(max_hints=self._max_hints)
        except Exception as exc:  # noqa: BLE001 — degrade rather than fail analysis
            log.warning("prompt_hints_unavailable", client=client_id, error=str(exc))
            hints = ""

        if self._ttl > 0:
            self._cache[client_id] = _CacheEntry(hints=hints, expires_at=now + self._ttl)
        return hints
