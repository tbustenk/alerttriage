"""OpenAI GPT backend for alert analysis."""

from __future__ import annotations

import json
from typing import Any

import openai

from alerttriage.src.core.alert_models import Alert
from alerttriage.src.core.result_models import (
    AnalysisResult, Verdict, RiskFactor, RecommendedAction,
)
from alerttriage.src.logger import get_logger

log = get_logger(__name__)

_SYSTEM_PROMPT = """\
You are a senior SOC analyst AI assistant. Triage the provided security alert
and respond ONLY with valid JSON matching this schema:

{
  "verdict": "true_positive|false_positive|needs_escalation|needs_investigation|benign|unknown",
  "confidence": 0.0-1.0,
  "summary": "one-sentence verdict summary",
  "reasoning": "detailed reasoning",
  "risk_factors": [{"name": "...", "description": "...", "weight": 0.0-1.0}],
  "recommended_actions": [{"priority": 1-5, "action": "...", "rationale": "..."}]
}
"""

_PRICING: dict[str, dict[str, float]] = {
    "gpt-4o":       {"input": 5.0,  "output": 15.0},
    "gpt-4o-mini":  {"input": 0.15, "output": 0.6},
    "gpt-4-turbo":  {"input": 10.0, "output": 30.0},
}


class GPTBackend:
    """Wraps the OpenAI SDK for alert triage."""

    def __init__(self, model_id: str, spec: dict[str, Any]) -> None:
        self.model_id = model_id
        self.model_name: str = spec.get("model_name", "gpt-4o-mini")
        self.max_tokens: int = spec.get("max_tokens", 1024)
        self.temperature: float = spec.get("temperature", 0.2)
        self._client = openai.AsyncOpenAI()

    async def analyze(self, alert: Alert) -> AnalysisResult:
        user_content = _build_user_message(alert)

        response = await self._client.chat.completions.create(
            model=self.model_name,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )

        raw_text = response.choices[0].message.content or ""
        parsed = _parse_response(raw_text)

        usage = response.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        cost = _estimate_cost(self.model_name, prompt_tokens, completion_tokens)

        return AnalysisResult(
            alert_id=alert.id,
            client_id=alert.client_id,
            model_id=self.model_id,
            verdict=Verdict(parsed.get("verdict", "unknown")),
            confidence=float(parsed.get("confidence", 0.5)),
            summary=parsed.get("summary", ""),
            reasoning=parsed.get("reasoning", ""),
            risk_factors=[RiskFactor(**r) for r in parsed.get("risk_factors", [])],
            recommended_actions=[
                RecommendedAction(**a) for a in parsed.get("recommended_actions", [])
            ],
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
        )


def _build_user_message(alert: Alert) -> str:
    return (
        f"ALERT ID: {alert.id}\n"
        f"Rule: {alert.rule_name}\n"
        f"Severity: {alert.severity.value}\n"
        f"Title: {alert.title}\n"
        f"Description: {alert.description}\n"
        f"Tags: {', '.join(alert.tags)}\n"
        f"Context:\n{json.dumps(alert.context.model_dump(), indent=2)}\n"
        f"Raw payload (excerpt):\n{json.dumps(dict(list(alert.raw_payload.items())[:20]), indent=2)}\n"
        "\nTriage this alert."
    )


def _parse_response(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        log.warning("gpt_parse_error", error=str(exc), raw=text[:200])
        return {}


def _estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    rates = _PRICING.get(model, {"input": 5.0, "output": 15.0})
    return (prompt_tokens * rates["input"] + completion_tokens * rates["output"]) / 1_000_000
