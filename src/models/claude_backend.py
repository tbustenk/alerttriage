"""Claude (Anthropic) AI backend for alert analysis."""

from __future__ import annotations

import json
from typing import Any

import anthropic

from alerttriage.src.core.alert_models import Alert
from alerttriage.src.core.result_models import AnalysisResult, Verdict, RiskFactor, RecommendedAction
from alerttriage.src.logger import get_logger

log = get_logger(__name__)

_SYSTEM_PROMPT = """\
You are a senior SOC analyst AI assistant. Your job is to triage security alerts
quickly and accurately. Respond ONLY with valid JSON matching the schema below.

Schema:
{
  "verdict": "true_positive|false_positive|needs_escalation|needs_investigation|benign|unknown",
  "confidence": 0.0-1.0,
  "summary": "one-sentence verdict summary",
  "reasoning": "detailed multi-line reasoning",
  "risk_factors": [{"name": "...", "description": "...", "weight": 0.0-1.0}],
  "recommended_actions": [{"priority": 1-5, "action": "...", "rationale": "..."}]
}

Rules:
- Base confidence on evidence quality, not severity alone.
- List at least one risk factor.
- List at least one recommended action.
- Do not hallucinate. If uncertain, use verdict=needs_investigation.
"""


class ClaudeBackend:
    """Wraps the Anthropic SDK; uses prompt caching for cost efficiency."""

    def __init__(self, model_id: str, spec: dict[str, Any]) -> None:
        self.model_id = model_id
        self.model_name: str = spec.get("model_name", "claude-sonnet-4-6")
        self.max_tokens: int = spec.get("max_tokens", 1024)
        self.temperature: float = spec.get("temperature", 0.2)
        self._client = anthropic.AsyncAnthropic()

    async def analyze(self, alert: Alert) -> AnalysisResult:
        user_content = _build_user_message(alert)

        response = await self._client.messages.create(
            model=self.model_name,
            max_tokens=self.max_tokens,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},  # prompt caching
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        )

        raw_text = response.content[0].text
        parsed = _parse_response(raw_text)

        usage = response.usage
        cost = _estimate_cost(
            self.model_name,
            prompt_tokens=usage.input_tokens,
            completion_tokens=usage.output_tokens,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0),
        )

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
            prompt_tokens=usage.input_tokens,
            completion_tokens=usage.output_tokens,
            cost_usd=cost,
        )


# ---------------------------------------------------------------------------
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
        start = text.index("{")
        end = text.rindex("}") + 1
        return json.loads(text[start:end])
    except (ValueError, json.JSONDecodeError) as exc:
        log.warning("claude_parse_error", error=str(exc), raw=text[:200])
        return {}


# Rough pricing per 1M tokens (update as Anthropic publishes new rates).
_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-7":    {"input": 15.0,  "output": 75.0,  "cache_read": 1.5},
    "claude-sonnet-4-6":  {"input": 3.0,   "output": 15.0,  "cache_read": 0.3},
    "claude-haiku-4-5-20251001": {"input": 0.8, "output": 4.0, "cache_read": 0.08},
}


def _estimate_cost(model: str, *, prompt_tokens: int, completion_tokens: int,
                   cache_read_tokens: int) -> float:
    rates = _PRICING.get(model, {"input": 3.0, "output": 15.0, "cache_read": 0.3})
    billable_input = max(0, prompt_tokens - cache_read_tokens)
    return (
        billable_input * rates["input"]
        + cache_read_tokens * rates["cache_read"]
        + completion_tokens * rates["output"]
    ) / 1_000_000
