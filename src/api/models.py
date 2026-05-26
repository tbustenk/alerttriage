"""Pydantic request/response models for the AlertTriage REST API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class AnalyzeRequest(BaseModel):
    """POST /analyze body."""

    client_id: str
    rule_name: str
    severity: str
    title: str
    description: str
    source: str = "manual"
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    source_alert_id: str | None = None
    dry_run: bool = False

    model_config = {"json_schema_extra": {
        "example": {
            "client_id": "acme-corp",
            "rule_name": "brute_force_login",
            "severity": "high",
            "title": "Multiple failed SSH logins",
            "description": "50 failed SSH login attempts from 1.2.3.4 in 5 minutes",
            "source": "splunk",
            "tags": ["ssh", "brute-force"],
        }
    }}


class FeedbackRequest(BaseModel):
    """POST /feedback body."""

    alert_id: str
    analysis_id: str
    client_id: str
    analyst_id: str
    analyst_verdict: Literal["true_positive", "false_positive", "escalated", "closed"]
    analyst_notes: str = ""
    ai_verdict_was_correct: bool
    rule_name: str = "unknown"
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"json_schema_extra": {
        "example": {
            "alert_id": "abc-123",
            "analysis_id": "def-456",
            "client_id": "acme-corp",
            "analyst_id": "analyst@acme.com",
            "analyst_verdict": "false_positive",
            "analyst_notes": "Known scanner, added to allowlist",
            "ai_verdict_was_correct": False,
            "rule_name": "port_scan_detected",
        }
    }}


class ContextRequest(BaseModel):
    """POST /context body."""

    client_id: str
    context_type: str  # network_ranges | known_services | threat_intel | custom
    data: dict[str, Any]
    description: str = ""

    model_config = {"json_schema_extra": {
        "example": {
            "client_id": "acme-corp",
            "context_type": "network_ranges",
            "data": {"internal_ranges": ["10.0.0.0/8", "192.168.0.0/16"]},
            "description": "Corporate internal network ranges",
        }
    }}


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class AnalysisResponse(BaseModel):
    """Returned by POST /analyze and GET /results/{alert_id}."""

    id: str
    alert_id: str
    client_id: str
    model_id: str
    verdict: str
    confidence: float
    summary: str
    reasoning: str
    risk_factors: list[dict[str, Any]] = Field(default_factory=list)
    recommended_actions: list[dict[str, Any]] = Field(default_factory=list)
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: int
    timestamp: datetime


class RuleStatsItem(BaseModel):
    rule_name: str
    total: int
    correct: int
    false_positives: int
    fp_rate: float
    accuracy: float
    latest_ts: str | None = None


class StatsResponse(BaseModel):
    """Returned by GET /stats/{client_id}."""

    client_id: str
    overall_accuracy: float
    total_analyses: int
    rule_stats: list[RuleStatsItem]


class HintsResponse(BaseModel):
    """Returned by GET /hints."""

    client_id: str
    hints: list[str]
    generated_at: datetime


class HealthComponentStatus(BaseModel):
    name: str
    healthy: bool
    latency_ms: float | None = None
    details: str = ""


class HealthResponse(BaseModel):
    """Returned by GET /health."""

    status: str  # healthy | degraded | unhealthy
    version: str
    uptime_seconds: float
    components: list[HealthComponentStatus]
    timestamp: datetime


class ErrorResponse(BaseModel):
    """Standard error envelope."""

    error: str
    detail: str = ""
    code: int
