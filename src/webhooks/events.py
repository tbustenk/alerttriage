"""Webhook event type definitions and payload models."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EventType(StrEnum):
    ALERT_ANALYZED      = "alert_analyzed"
    FEEDBACK_RECORDED   = "feedback_recorded"
    ACCURACY_CHANGED    = "accuracy_changed"
    THRESHOLD_EXCEEDED  = "threshold_exceeded"
    REPORT_GENERATED    = "report_generated"
    COST_LIMIT_APPROACHING = "cost_limit_approaching"


class WebhookRegistration(BaseModel):
    """A client's registered webhook endpoint."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_id: str
    url: str
    events: list[str]   # EventType values that trigger this webhook
    secret: str | None = None   # HMAC-SHA256 signing secret
    description: str = ""
    active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    consecutive_failures: int = 0

    model_config = {"json_schema_extra": {
        "example": {
            "client_id": "acme-corp",
            "url": "https://hooks.slack.com/services/...",
            "events": ["alert_analyzed", "threshold_exceeded"],
            "secret": "my-hmac-secret",
            "description": "SOC Slack channel notifications",
        }
    }}


class WebhookEvent(BaseModel):
    """The payload envelope posted to registered webhooks."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: str
    client_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    data: dict[str, Any]

    model_config = {"json_schema_extra": {
        "example": {
            "id": "evt-abc123",
            "event_type": "alert_analyzed",
            "client_id": "acme-corp",
            "timestamp": "2026-05-25T10:30:00Z",
            "data": {
                "alert_id": "abc-123",
                "verdict": "true_positive",
                "confidence": 0.92,
                "rule_name": "brute_force_login",
                "severity": "high",
                "summary": "50 failed SSH logins from 1.2.3.4",
            },
        }
    }}


class DeliveryResult(BaseModel):
    """Outcome of a single webhook delivery attempt."""

    webhook_id: str
    event_id: str
    success: bool
    status_code: int | None = None
    attempts: int = 1
    error: str | None = None
    delivered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Event factory helpers
# ---------------------------------------------------------------------------


def make_alert_analyzed_event(client_id: str, result: Any) -> WebhookEvent:
    """Build an ``alert_analyzed`` event from an AnalysisResult."""
    return WebhookEvent(
        event_type=EventType.ALERT_ANALYZED,
        client_id=client_id,
        data={
            "alert_id":   result.alert_id,
            "analysis_id": result.id,
            "verdict":    result.verdict.value if hasattr(result.verdict, "value") else result.verdict,
            "confidence": result.confidence,
            "model_id":   result.model_id,
            "cost_usd":   result.cost_usd,
            "latency_ms": result.latency_ms,
            "summary":    result.summary[:500],
        },
    )


def make_feedback_event(client_id: str, feedback: Any) -> WebhookEvent:
    """Build a ``feedback_recorded`` event from a FeedbackRecord."""
    return WebhookEvent(
        event_type=EventType.FEEDBACK_RECORDED,
        client_id=client_id,
        data={
            "alert_id":        feedback.alert_id,
            "analysis_id":     feedback.analysis_id,
            "analyst_id":      feedback.analyst_id,
            "analyst_verdict": feedback.analyst_verdict,
            "ai_was_correct":  feedback.ai_verdict_was_correct,
        },
    )


def make_threshold_event(
    client_id: str,
    metric: str,
    threshold: float,
    current: float,
    message: str,
) -> WebhookEvent:
    return WebhookEvent(
        event_type=EventType.THRESHOLD_EXCEEDED,
        client_id=client_id,
        data={
            "metric":    metric,
            "threshold": threshold,
            "current":   current,
            "message":   message,
        },
    )
