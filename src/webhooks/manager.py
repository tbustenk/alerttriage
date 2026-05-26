"""WebhookManager — register endpoints, persist state, emit events.

Registrations are persisted to ``data/webhooks.json`` so they survive
restarts without a database migration. Delivery is async and fire-and-forget
by default (callers await ``emit()`` only if they need delivery results).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from alerttriage.src.logger import get_logger
from alerttriage.src.webhooks.delivery import deliver
from alerttriage.src.webhooks.events import (
    DeliveryResult,
    EventType,
    WebhookEvent,
    WebhookRegistration,
)

log = get_logger(__name__)

_REGISTRY_FILENAME = "webhooks.json"
_MAX_CONSECUTIVE_FAILURES = 10   # disable webhook after this many in a row


class WebhookManager:
    """Manages webhook registrations and dispatches events.

    Args:
        data_dir: Root data directory.  Registry is stored at
            ``data/webhooks.json``.
    """

    def __init__(self, data_dir: Path) -> None:
        self._path = Path(data_dir) / _REGISTRY_FILENAME
        self._registrations: dict[str, WebhookRegistration] = {}
        self._lock = Lock()
        self._load()

    # ------------------------------------------------------------------
    # Registration CRUD
    # ------------------------------------------------------------------

    def register(
        self,
        client_id: str,
        url: str,
        events: list[str],
        *,
        secret: str | None = None,
        description: str = "",
    ) -> WebhookRegistration:
        """Create and persist a new webhook registration."""
        reg = WebhookRegistration(
            client_id=client_id,
            url=url,
            events=[str(e) for e in events],
            secret=secret,
            description=description,
        )
        with self._lock:
            self._registrations[reg.id] = reg
            self._save()
        log.info("webhook_registered", id=reg.id, client=client_id, url=url, events=events)
        return reg

    def deregister(self, webhook_id: str) -> bool:
        """Remove a webhook registration. Returns True if it existed."""
        with self._lock:
            existed = webhook_id in self._registrations
            if existed:
                del self._registrations[webhook_id]
                self._save()
        return existed

    def get(self, webhook_id: str) -> WebhookRegistration | None:
        return self._registrations.get(webhook_id)

    def list_for_client(self, client_id: str) -> list[WebhookRegistration]:
        return [r for r in self._registrations.values() if r.client_id == client_id]

    def list_all(self) -> list[WebhookRegistration]:
        return list(self._registrations.values())

    def update(self, webhook_id: str, **kwargs: Any) -> WebhookRegistration | None:
        """Partial update of an existing registration."""
        with self._lock:
            reg = self._registrations.get(webhook_id)
            if reg is None:
                return None
            updated = reg.model_copy(update=kwargs)
            self._registrations[webhook_id] = updated
            self._save()
        return updated

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    async def emit(
        self,
        event: WebhookEvent,
        *,
        fire_and_forget: bool = True,
    ) -> list[DeliveryResult]:
        """Dispatch *event* to all matching webhooks.

        Args:
            event: The event to dispatch.
            fire_and_forget: When True (default), scheduling delivery as asyncio
                tasks and returning immediately with an empty list.  When False,
                waits for all deliveries and returns results — useful for
                ``POST /webhooks/test``.
        """
        targets = [
            r for r in self._registrations.values()
            if r.client_id == event.client_id
            and event.event_type in r.events
            and r.active
        ]

        if not targets:
            return []

        if fire_and_forget:
            for reg in targets:
                asyncio.create_task(self._deliver_and_update(reg, event))
            return []

        results = await asyncio.gather(
            *[self._deliver_and_update(reg, event) for reg in targets],
            return_exceptions=False,
        )
        return list(results)

    async def test_delivery(self, webhook_id: str) -> DeliveryResult:
        """Send a synthetic test event to validate the webhook endpoint."""
        reg = self._registrations.get(webhook_id)
        if reg is None:
            return DeliveryResult(
                webhook_id=webhook_id,
                event_id="test",
                success=False,
                error="Webhook not found",
            )
        test_event = WebhookEvent(
            event_type=EventType.ALERT_ANALYZED,
            client_id=reg.client_id,
            data={"test": True, "message": "AlertTriage webhook test"},
        )
        return await deliver(reg, test_event)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _deliver_and_update(
        self,
        registration: WebhookRegistration,
        event: WebhookEvent,
    ) -> DeliveryResult:
        result = await deliver(registration, event)

        now = datetime.now(timezone.utc)
        with self._lock:
            reg = self._registrations.get(registration.id)
            if reg is None:
                return result
            if result.success:
                updated = reg.model_copy(update={
                    "last_success_at": now,
                    "consecutive_failures": 0,
                })
            else:
                new_failures = reg.consecutive_failures + 1
                active = new_failures < _MAX_CONSECUTIVE_FAILURES
                if not active:
                    log.warning(
                        "webhook_auto_disabled",
                        id=registration.id,
                        failures=new_failures,
                    )
                updated = reg.model_copy(update={
                    "last_failure_at": now,
                    "consecutive_failures": new_failures,
                    "active": active,
                })
            self._registrations[registration.id] = updated
            self._save()
        return result

    def _save(self) -> None:
        data = [r.model_dump(mode="json") for r in self._registrations.values()]
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        tmp.replace(self._path)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for item in raw:
                reg = WebhookRegistration(**item)
                self._registrations[reg.id] = reg
            log.info("webhooks_loaded", count=len(self._registrations))
        except Exception as exc:  # noqa: BLE001
            log.error("webhooks_load_failed", error=str(exc))
