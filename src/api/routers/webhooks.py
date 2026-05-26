"""Webhook management API router — /webhooks/..."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from alerttriage.src.api.auth import require_api_key
from alerttriage.src.webhooks.events import EventType, WebhookRegistration

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


def _manager() -> Any:
    from alerttriage.src.api.app import _webhook_manager  # type: ignore[attr-defined]
    if _webhook_manager is None:
        raise HTTPException(status_code=503, detail="Webhook manager not initialised")
    return _webhook_manager


class _RegisterRequest(WebhookRegistration.__pydantic_settings__ if hasattr(WebhookRegistration, '__pydantic_settings__') else object):  # type: ignore
    pass


from pydantic import BaseModel


class RegisterRequest(BaseModel):
    client_id: str
    url: str
    events: list[str]
    secret: str | None = None
    description: str = ""


class UpdateRequest(BaseModel):
    url: str | None = None
    events: list[str] | None = None
    secret: str | None = None
    description: str | None = None
    active: bool | None = None


@router.get("/event-types", summary="List all valid event types")
async def list_event_types() -> list[str]:
    """Return all event type strings that can be used in webhook subscriptions."""
    return [e.value for e in EventType]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Register a webhook endpoint",
)
async def register_webhook(
    body: RegisterRequest,
    _key: str = Depends(require_api_key),
) -> dict[str, Any]:
    """
    Register an endpoint to receive AlertTriage events.

    Valid ``events`` values:
    - ``alert_analyzed``       — fired after every AI triage
    - ``feedback_recorded``    — fired when an analyst records a verdict
    - ``accuracy_changed``     — fired when client accuracy crosses a threshold
    - ``threshold_exceeded``   — fired when a health threshold is breached
    - ``report_generated``     — fired when a scheduled report is sent
    - ``cost_limit_approaching``— fired when spend approaches 90% of limit

    When ``secret`` is provided, the payload is signed with HMAC-SHA256 and
    the signature is sent in the ``X-AlertTriage-Signature: sha256=<hex>`` header.
    """
    valid_events = {e.value for e in EventType}
    invalid = set(body.events) - valid_events
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid event types: {sorted(invalid)}. Valid: {sorted(valid_events)}",
        )

    mgr = _manager()
    reg = mgr.register(
        client_id=body.client_id,
        url=body.url,
        events=body.events,
        secret=body.secret,
        description=body.description,
    )
    return _reg_to_dict(reg)


@router.get("", summary="List all webhooks (optionally filtered by client)")
async def list_webhooks(
    client_id: str | None = None,
    _key: str = Depends(require_api_key),
) -> list[dict[str, Any]]:
    mgr = _manager()
    regs = mgr.list_for_client(client_id) if client_id else mgr.list_all()
    return [_reg_to_dict(r) for r in regs]


@router.get("/{webhook_id}", summary="Get a webhook registration")
async def get_webhook(
    webhook_id: str,
    _key: str = Depends(require_api_key),
) -> dict[str, Any]:
    mgr = _manager()
    reg = mgr.get(webhook_id)
    if reg is None:
        raise HTTPException(status_code=404, detail=f"Webhook '{webhook_id}' not found")
    return _reg_to_dict(reg)


@router.patch("/{webhook_id}", summary="Update a webhook registration")
async def update_webhook(
    webhook_id: str,
    body: UpdateRequest,
    _key: str = Depends(require_api_key),
) -> dict[str, Any]:
    mgr = _manager()
    updates = body.model_dump(exclude_none=True)
    reg = mgr.update(webhook_id, **updates)
    if reg is None:
        raise HTTPException(status_code=404, detail=f"Webhook '{webhook_id}' not found")
    return _reg_to_dict(reg)


@router.delete("/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a webhook")
async def delete_webhook(
    webhook_id: str,
    _key: str = Depends(require_api_key),
) -> None:
    mgr = _manager()
    if not mgr.deregister(webhook_id):
        raise HTTPException(status_code=404, detail=f"Webhook '{webhook_id}' not found")


@router.post("/{webhook_id}/test", summary="Send a test event to a webhook")
async def test_webhook(
    webhook_id: str,
    _key: str = Depends(require_api_key),
) -> dict[str, Any]:
    """Deliver a synthetic ``alert_analyzed`` test event and return the result."""
    mgr = _manager()
    result = await mgr.test_delivery(webhook_id)
    return {
        "webhook_id": result.webhook_id,
        "event_id": result.event_id,
        "success": result.success,
        "status_code": result.status_code,
        "attempts": result.attempts,
        "error": result.error,
        "delivered_at": result.delivered_at.isoformat(),
    }


def _reg_to_dict(reg: WebhookRegistration) -> dict[str, Any]:
    return {
        "id": reg.id,
        "client_id": reg.client_id,
        "url": reg.url,
        "events": reg.events,
        "description": reg.description,
        "active": reg.active,
        "has_secret": bool(reg.secret),
        "consecutive_failures": reg.consecutive_failures,
        "last_success_at": reg.last_success_at.isoformat() if reg.last_success_at else None,
        "last_failure_at": reg.last_failure_at.isoformat() if reg.last_failure_at else None,
        "created_at": reg.created_at.isoformat(),
    }
