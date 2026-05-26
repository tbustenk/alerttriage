"""Config hot-reload: watch YAML files and refresh ConfigManager on change.

Uses ``watchdog`` when available; falls back to periodic mtime polling so the
service degrades gracefully if watchdog is not installed.

Usage in the API lifespan:

    watcher = ConfigWatcher(config, config_dir, on_change=on_config_changed)
    asyncio.create_task(watcher.run())
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any, Callable

from alerttriage.src.logger import get_logger

log = get_logger(__name__)

_POLL_INTERVAL = 5.0   # seconds between mtime checks when watchdog absent


class ConfigWatcher:
    """Watches config files and calls *on_change* when any YAML is modified.

    Args:
        config:       ConfigManager to reload on change.
        config_dir:   Directory to watch (recursive).
        on_change:    Optional async callback ``(changed_path: Path) -> None``.
        poll_interval: Fallback poll interval in seconds.
    """

    def __init__(
        self,
        config: Any,
        config_dir: Path,
        *,
        on_change: Callable[[Path], Any] | None = None,
        poll_interval: float = _POLL_INTERVAL,
    ) -> None:
        self._config = config
        self._config_dir = Path(config_dir)
        self._on_change = on_change
        self._poll_interval = poll_interval
        self._mtimes: dict[Path, float] = {}
        self._running = False

    async def run(self) -> None:
        """Start watching. Runs until cancelled or stop() is called."""
        self._running = True
        self._snapshot_mtimes()

        try:
            await self._run_watchdog()
        except ImportError:
            log.info(
                "watchdog_not_installed",
                detail="Using polling fallback. Install watchdog for inotify-based watching.",
            )
            await self._run_polling()

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Watchdog-based watching (preferred)
    # ------------------------------------------------------------------

    async def _run_watchdog(self) -> None:
        from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent
        from watchdog.observers import Observer

        watcher = self

        class _Handler(FileSystemEventHandler):
            def on_modified(self, event):
                if not event.is_directory and event.src_path.endswith(".yaml"):
                    asyncio.run_coroutine_threadsafe(
                        watcher._handle_change(Path(event.src_path)),
                        asyncio.get_event_loop(),
                    )

            def on_created(self, event):
                self.on_modified(event)

        observer = Observer()
        observer.schedule(_Handler(), str(self._config_dir), recursive=True)
        observer.start()
        log.info("config_watchdog_started", directory=str(self._config_dir))

        try:
            while self._running:
                await asyncio.sleep(1)
        finally:
            observer.stop()
            observer.join()

    # ------------------------------------------------------------------
    # Polling fallback
    # ------------------------------------------------------------------

    async def _run_polling(self) -> None:
        log.info(
            "config_poll_watcher_started",
            directory=str(self._config_dir),
            interval=self._poll_interval,
        )
        while self._running:
            await asyncio.sleep(self._poll_interval)
            await self._check_changes()

    async def _check_changes(self) -> None:
        for yaml_path in self._config_dir.rglob("*.yaml"):
            try:
                mtime = yaml_path.stat().st_mtime
            except OSError:
                continue
            if self._mtimes.get(yaml_path) != mtime:
                self._mtimes[yaml_path] = mtime
                await self._handle_change(yaml_path)

    async def _handle_change(self, path: Path) -> None:
        log.info("config_file_changed", path=str(path))
        try:
            self._reload_config(path)
        except Exception as exc:  # noqa: BLE001
            log.error("config_reload_failed", path=str(path), error=str(exc))
            return

        if self._on_change:
            result = self._on_change(path)
            if asyncio.iscoroutine(result):
                await result

    def _reload_config(self, changed_path: Path) -> None:
        """Invalidate cached client config when its file changes."""
        config = self._config

        # Clear client cache for the changed file
        if "client_configs" in str(changed_path):
            client_id = changed_path.stem
            if hasattr(config, "_client_cache"):
                config._client_cache.pop(client_id, None)
            log.info("client_config_reloaded", client=client_id)
        elif changed_path.name == "default_config.yaml":
            if hasattr(config, "_default"):
                config._default = config._load_default()
            log.info("default_config_reloaded")
        elif changed_path.name == "models.yaml":
            if hasattr(config, "_models"):
                config._models = config._load_models()
            log.info("models_config_reloaded")

    def _snapshot_mtimes(self) -> None:
        for yaml_path in self._config_dir.rglob("*.yaml"):
            try:
                self._mtimes[yaml_path] = yaml_path.stat().st_mtime
            except OSError:
                pass
