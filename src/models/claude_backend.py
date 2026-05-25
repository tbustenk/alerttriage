"""Claude (Anthropic) AI backend for alert analysis.

Uses Anthropic prompt caching: the system prompt is marked
``cache_control: ephemeral`` so repeated triage calls bill at the cache-read
rate (about 10% of the input rate). Don't vary the system prompt per
request — the cache key changes and you pay full input price every time.

The :class:`alerttriage.src.feedback.prompt_enhancer.PromptEnhancer`
prepends per-client learning hints when present, but those hints are stable
within a TTL window so the cache survives most reads.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic

from alerttriage.src.core.alert_models import Alert
from alerttriage.src.core.result_models import (
    AnalysisResult,
    RecommendedAction,
    RiskFactor,
    Verdict,
)
from alerttriage.src.logger import get_logger
from alerttriage.src.retry import (
    RetryConfigLike,
    classify_anthropic_error,
    with_retry,
)

log = get_logger(__name__)

BASE_SYSTEM_PROMPT = """\
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
        """Construct a backend from a ``models.yaml`` entry."""
        self.model_id = model_id
        self.model_name: str = spec.get("model_name", "claude-sonnet-4-6")
        self.max_tokens: int = int(spec.get("max_tokens", 1024))
        self.temperature: float = float(spec.get("temperature", 0.2))
        self._client = anthropic.AsyncAnthropic()

    async def analyze(
        self,
        alert: Alert,
        *,
        system_prompt: str = BASE_SYSTEM_PROMPT,
        retry_config: RetryConfigLike | None = None,
    ) -> AnalysisResult:
        """Send a single alert to Claude and parse the JSON verdict.

        Args:
            alert: Anonymised alert to analyse.
            system_prompt: System prompt to send. Defaults to the base prompt;
                pass the enhanced prompt from :class:`PromptEnhancer` to inject
                client-specific guidance.
            retry_config: Retry policy. When ``None``, calls run once without
                automatic backoff (caller has already given up its budget).
        """
        user_content = _build_user_message(alert)

        async def _call() -> Any:
            try:
                return await self._client.messages.create(
                    model=self.model_name,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    system=[
                        {
                            "type": "text",
                            "text": system_prompt,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    messages=[{"role": "user", "content": user_content}],
                )
            except Exception as exc:  # noqa: BLE001 — classify then re-raise
                raise classify_anthropic_error(exc) from exc

        if retry_config is None:
            response = await _call()
        else:
            response = await with_retry(
                _call,
                config=retry_config,
                op_name=f"claude:{self.model_name}",
            )

        raw_text = response.content[0].text if response.content else ""
        parsed = _parse_response(raw_text)

        usage = response.usage
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cost = _estimate_cost(
            self.model_name,
            prompt_tokens=int(usage.input_tokens),
            completion_tokens=int(usage.output_tokens),
            cache_read_tokens=cache_read,
        )

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
            prompt_tokens=int(usage.input_tokens),
            completion_tokens=int(usage.output_tokens),
            cost_usd=cost,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_user_message(alert: Alert) -> str:
    """Render an alert into the user-turn payload."""
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
    """Extract a JSON object from the model's text output.

    Returns an empty dict (which downstream defaults handle) when the response
    isn't valid JSON, rather than letting the worker crash mid-batch.
    """
    if not text:
        return {}
    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        return json.loads(text[start:end])
    except (ValueError, json.JSONDecodeError) as exc:
        log.warning("claude_parse_error", error=str(exc), raw=text[:200])
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
    weight = item.get("weight", 0.0)
    try:
        w = min(max(float(weight), 0.0), 1.0)
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
    priority = item.get("priority", 3)
    try:
        p = max(1, min(5, int(priority)))
    except (TypeError, ValueError):
        p = 3
    return RecommendedAction(
        priority=p,
        action=str(item.get("action", "review alert"))[:500],
        rationale=str(item.get("rationale", ""))[:500],
    )


# Rough pricing per 1M tokens (update as Anthropic publishes new rates).
_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-7": {"input": 15.0, "output": 75.0, "cache_read": 1.5},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.3},
    "claude-haiku-4-5-20251001": {"input": 0.8, "output": 4.0, "cache_read": 0.08},
}


def _estimate_cost(
    model: str,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cache_read_tokens: int,
) -> float:
    """Compute a USD estimate. Falls back to Sonnet pricing for unknown models."""
    rates = _PRICING.get(model, {"input": 3.0, "output": 15.0, "cache_read": 0.3})
    billable_input = max(0, prompt_tokens - cache_read_tokens)
    return (
        billable_input * rates["input"]
        + cache_read_tokens * rates["cache_read"]
        + completion_tokens * rates["output"]
    ) / 1_000_000
