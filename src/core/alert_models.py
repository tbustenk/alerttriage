"""Pydantic models for alerts, context, and feedback records."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class AlertSeverity(StrEnum):
    """Severity levels recognised across all SIEM sources."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class AlertSource(StrEnum):
    """Where an alert originated."""

    SPLUNK = "splunk"
    ELK = "elk"
    WEBHOOK = "webhook"
    MANUAL = "manual"
    SIEM_GENERIC = "siem_generic"


class AlertContext(BaseModel):
    """Enrichment data attached to an alert."""

    host_info: dict[str, Any] = Field(default_factory=dict)
    user_info: dict[str, Any] = Field(default_factory=dict)
    network_info: dict[str, Any] = Field(default_factory=dict)
    threat_intel: dict[str, Any] = Field(default_factory=dict)
    related_alerts: list[str] = Field(default_factory=list)
    raw_logs: list[str] = Field(default_factory=list)


class Alert(BaseModel):
    """Normalised alert record ingested from any SIEM source."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_id: str
    source: AlertSource
    rule_name: str
    severity: AlertSeverity
    title: str
    description: str
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    context: AlertContext = Field(default_factory=AlertContext)
    tags: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    source_alert_id: str | None = None

    @field_validator("client_id")
    @classmethod
    def client_id_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("client_id must not be blank")
        return v.strip().lower()


class FeedbackRecord(BaseModel):
    """Analyst decision recorded for a previously triaged alert."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    alert_id: str
    analysis_id: str
    client_id: str
    analyst_id: str
    analyst_verdict: str  # true_positive / false_positive / escalated / closed
    analyst_notes: str = ""
    ai_verdict_was_correct: bool
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)
