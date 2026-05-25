# Feedback loop

AlertTriage's feedback loop converts analyst decisions into prompt hints
that steer future analyses without retraining a model. This document
explains how the pieces fit together, what tunes the behaviour, and how to
introduce or wipe feedback safely.

## Data flow

```
Analyst marks alert in SOAR
         │
         ▼ POST { verdict, was_ai_correct, … }
   FeedbackSystem.record_many()        (data/<client>/feedback.db)
         │
         ▼   per-rule aggregate SQL (no per-row JSON deserialise)
   LearningEngine.compute_rule_insights()
         │
         ▼   filters: fp_rate ≥ threshold AND samples ≥ min
   LearningEngine.generate_prompt_hints()
         │
         ▼   memoised per client with TTL
   PromptEnhancer.enhance(base_prompt, client_id=…)
         │
         ▼
   ClaudeBackend / GPTBackend system prompt   ← used on next analysis
```

The components:

* **`FeedbackSystem`** ([src/feedback/feedback_system.py](../src/feedback/feedback_system.py)) is a per-client SQLite store. `rule_name` is a real column with its own index, so aggregations don't pay the JSON-deserialise tax that earlier versions did.
* **`LearningEngine`** ([src/feedback/learning_engine.py](../src/feedback/learning_engine.py)) runs an aggregate query and surfaces rules where the AI is consistently wrong.
* **`PromptEnhancer`** ([src/feedback/prompt_enhancer.py](../src/feedback/prompt_enhancer.py)) caches the generated hints (default 5 min TTL) so the hot path never blocks on the feedback DB.
* **`AlertAnalyzer`** ([src/core/analyzer.py](../src/core/analyzer.py)) wires the enhancer into every triage call.

## Knobs

All three live under `learning:` in `default_config.yaml` or per-client overrides:

```yaml
learning:
  fp_rate_threshold: 0.50   # only flag rules with this FP rate or higher
  min_sample_size:   5      # require this many feedback rows before trusting the rate
  max_hints:         10     # cap how many hint lines get prepended
  cache_ttl_sec:     300    # prompt-hint cache TTL — also controls Anthropic cache hit rate
```

Lower `fp_rate_threshold` (e.g. 0.30) for noisy SIEMs that you want the AI
to be very conservative about; raise `min_sample_size` (e.g. 15) for
clients where the same rule fires hundreds of times a day and you can
afford to wait for more data.

## Recording feedback

Use `FeedbackSystem.record_many` for any non-trivial number of records — it
batches into a single SQLite transaction. Per-record `record()` is fine for
streaming a webhook-driven SOAR back-channel but is wasteful for nightly
sync jobs from a ticketing export.

```python
from pathlib import Path
from alerttriage.src.feedback.feedback_system import FeedbackSystem
from alerttriage.src.core.alert_models import FeedbackRecord

store = FeedbackSystem(Path("data"), "acme-corp")
store.record_many([
    FeedbackRecord(
        alert_id="alert-123",
        analysis_id="analysis-456",
        client_id="acme-corp",
        analyst_id="alice@acme-corp",
        analyst_verdict="false_positive",
        ai_verdict_was_correct=False,
        metadata={"rule_name": "Brute Force Login"},
    ),
    # …
])
```

After a bulk import, call `analyzer.invalidate_prompt_cache("acme-corp")`
so the next triage call rebuilds hints from the updated data.

## Failure modes

| What goes wrong | What happens |
|---|---|
| Feedback DB is corrupt on disk | `FeedbackSystem._init_db` archives it as `feedback.corrupt-<ts>` and re-creates an empty DB. Analysis continues. |
| Feedback store is unreadable when generating hints | `PromptEnhancer._get_hints` logs `prompt_hints_unavailable` and falls back to the base prompt. Analysis continues. |
| New feedback recorded but cache still warm | Hints are stale until `cache_ttl_sec` elapses, or until you call `analyzer.invalidate_prompt_cache(client_id)`. |

## Cache and Anthropic prompt caching

The TTL serves a second purpose. Anthropic's prompt cache keys on the
system prompt; if the prepended hints change every request the cache miss
rate goes up and your spend rises 10×. Keeping hints stable over a TTL
window (5 minutes is a sane default) lets the cache survive most of the
day's traffic. Only invalidate manually after big feedback imports.
