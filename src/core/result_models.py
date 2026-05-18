"""Models for analysis results returned by the AI engine."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Verdict(str, Enum):
    TRUE_POSITIVE = "true_positive"
    FALSE_POSITIVE = "false_positive"
    NEEDS_ESCALATION = "needs_escalation"
    NEEDS_INVESTIGATION = "needs_investigation"
    BENIGN = "benign"
    UNKNOWN = "unknown"


class RiskFactor(BaseModel):
    name: str
    description: str
    weight: float = Field(ge=0.0, le=1.0)


class RecommendedAction(BaseModel):
    priority: int = Field(ge=1, le=5)
    action: str
    rationale: str


class AnalysisResult(BaseModel):
    """Full output of one alert analysis pass."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    alert_id: str
    client_id: str
    model_id: str
    verdict: Verdict
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
    reasoning: str
    risk_factors: list[RiskFactor] = Field(default_factory=list)
    recommended_actions: list[RecommendedAction] = Field(default_factory=list)
    similar_past_alerts: list[str] = Field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens
