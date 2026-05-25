"""OpenAI GPT backend for alert analysis.

Uses ``response_format=json_object`` so the model returns parseable JSON
without prose. Like :mod:`claude_backend`, the system prompt is overridable
so :class:`alerttriage.src.feedback.prompt_enhancer.PromptEnhancer` can
prepend per-client learning hints.
"""

from __future__ import annotations

import json
from typing import Any

import openai

from alerttriage.src.core.alert_models import Alert
from alerttriage.src.core.result_models import (
    AnalysisResult,
    RecommendedAction,
    RiskFactor,
    Verdict,
)
from alerttriage.src.logger import get_logger
from alerttriage.src.retry import RetryConfigLike, classify_openai_error, with_retry

log = get_logger(__name__)

BASE_SYSTEM_PROMPT = """\
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
    "gpt-4o": {"input": 5.0, "output": 15.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.6},
    "gpt-4-turbo": {"input": 10.0, "output": 30.0},
}


class GPTBackend:
    """Wraps the OpenAI SDK for alert triage."""

    def __init__(self, model_id: str, spec: dict[str, Any]) -> None:
        """Construct a backend from a ``models.yaml`` entry."""
        self.model_id = model_id
        self.model_name: str = spec.get("model_name", "gpt-4o-mini")
        self.max_tokens: int = int(spec.get("max_tokens", 1024))
        self.temperature: float = float(spec.get("temperature", 0.2))
        self._client = openai.AsyncOpenAI()

    async def analyze(
        self,
        alert: Alert,
        *,
        system_prompt: str = BASE_SYSTEM_PROMPT,
        retry_config: RetryConfigLike | None = None,
    ) -> AnalysisResult:
        """Send a single alert to GPT and parse the JSON verdict."""
        user_content = _build_user_message(alert)

        async def _call() -> Any:
            try:
                return await self._client.chat.completions.create(
                    model=self.model_name,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                )
            except Exception as exc:  # noqa: BLE001 — classify then re-raise
                raise classify_openai_error(exc) from exc

        if retry_config is None:
            response = await _call()
        else:
            response = await with_retry(
                _call,
                config=retry_config,
                op_name=f"openai:{self.model_name}",
            )

        raw_text = response.choices[0].message.content or ""
        parsed = _parse_response(raw_text)

        usage = response.usage
        prompt_tokens = int(usage.prompt_tokens) if usage else 0
        completion_tokens = int(usage.completion_tokens) if usage else 0
        cost = _estimate_cost(self.model_name, prompt_tokens, completion_tokens)

        return AnalysisResult(
            alert_id=alert.id,
            client_id=alert.client_id,
            model_id=self.model_id,
            verdict=_safe_verdict(parsed.get("verdict")),
            confidence=_safe_confidence(parsed.get("confidence")),
            summary=str(parsed.get("summary", ""))[:1000],
            reasoning=str(parsed.get("reasoning", ""))[:8000],
            risk_factors=[_safe_risk_factor(r) for r in parsed.get("risk_factors", []) or []],
            recommended_actions=[
                _safe_recommended_action(a) for a in parsed.get("recommended_actions", []) or []
            ],
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
        )


# ---------------------------------------------------------------------------
# Helpers (mirror claude_backend so both surfaces stay consistent)
# ---------------------------------------------------------------------------


def _build_user_message(alert: Alert) -> str:
    context = json.dumps(alert.context.model_dump(), indent=2, default=str)
    payload_excerpt = json.dumps(dict(list(alert.raw_payload.items())[:20]), indent=2, default=str)
    return (
        f"ALERT ID: {alert.id}\n"
        f"Rule: {alert.rule_name}\n"
        f"Severity: {alert.severity.value}\n"
        f"Title: {alert.title}\n"
        f"Description: {alert.description}\n"
        f"Tags: {', '.join(alert.tags)}\n"
        f"Context:\n{context}\n"
        f"Raw payload (excerpt):\n{payload_excerpt}\n"
        "\nTriage this alert."
    )


def _parse_response(text: str) -> dict[str, Any]:
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        log.warning("gpt_parse_error", error=str(exc), raw=text[:200])
        return {}


def _safe_verdict(raw: object) -> Verdict:
    try:
        return Verdict(str(raw))
    except (ValueError, TypeError):
        return Verdict.UNKNOWN


def _safe_confidence(raw: object) -> float:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.5
    return min(max(value, 0.0), 1.0)


def _safe_risk_factor(item: object) -> RiskFactor:
    if not isinstance(item, dict):
        return RiskFactor(name="unknown", description="", weight=0.0)
    try:
        w = min(max(float(item.get("weight", 0.0)), 0.0), 1.0)
    except (TypeError, ValueError):
        w = 0.0
    return RiskFactor(
        name=str(item.get("name", "unknown"))[:200],
        description=str(item.get("description", ""))[:500],
        weight=w,
    )


def _safe_recommended_action(item: object) -> RecommendedAction:
    if not isinstance(item, dict):
        return RecommendedAction(priority=3, action="review alert", rationale="")
    try:
        p = max(1, min(5, int(item.get("priority", 3))))
    except (TypeError, ValueError):
        p = 3
    return RecommendedAction(
        priority=p,
        action=str(item.get("action", "review alert"))[:500],
        rationale=str(item.get("rationale", ""))[:500],
    )


def _estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Compute a USD estimate. Falls back to GPT-4o pricing for unknown models."""
    rates = _PRICING.get(model, {"input": 5.0, "output": 15.0})
    return (prompt_tokens * rates["input"] + completion_tokens * rates["output"]) / 1_000_000
