# AlertTriage v2 — Architecture

## System overview

AlertTriage v2 is a multi-tenant AI alert triage platform. It ingests
security alerts from SIEM systems, anonymises PII, routes them to a
configurable AI model, and writes verdicts back to the SIEM (or a
webhook). Analyst feedback is captured and converted into prompt hints
that steer future analyses.

```
SIEM (Splunk / ELK / Webhook)
        │
        ▼
  ┌──────────────────────────────────────────────────────────────┐
  │                       AlertAnalyzer                          │
  │                                                              │
  │  1. CostController.check_limit()        — daily/monthly cap  │
  │  2. Anonymizer.anonymize()              — strips PII         │
  │  3. PromptEnhancer.enhance()            — injects FB hints   │
  │  4. ModelRouter.route()                 — picks backend      │
  │     ├─ ClaudeBackend  (Anthropic SDK, prompt caching)        │
  │     └─ GPTBackend     (OpenAI SDK, JSON mode)                │
  │     with shared retry + rate-limit handling (src/retry.py)   │
  │  5. CostController.record()             — accounting         │
  └──────────────────────────────────────────────────────────────┘
        │
        ▼
  AnalysisResult (verdict + confidence + reasoning + cost)
        │
        ├──▶ SIEM connector  (write verdict back)
        ├──▶ WebhookOutput   (SOAR / ticketing)
        └──▶ FeedbackSystem  (store for next-run prompt hints)
```

## On-disk layout

The package directory is **flat** — `config/`, `src/`, `scripts/`,
`tests/` sit directly under the repo root. Imports use the dotted form
`alerttriage.src.*` and `alerttriage.config.*`. For those imports to
resolve, the *parent* of the repo (not the repo itself) must be on
`sys.path`, and the repo directory must literally be named `alerttriage`.
`pip install -e .` is the supported install path; `scripts/run_client.py`
also handles the import-path setup so direct invocation works.

```
alerttriage/                     ← repo root (must be named "alerttriage")
├── config/
│   ├── config_manager.py        # Loads + validates YAML, exposes typed views
│   ├── default_config.yaml      # System-wide defaults
│   ├── models.yaml              # AI model registry
│   └── client_configs/          # One YAML per client (+ examples/)
├── src/
│   ├── core/
│   │   ├── analyzer.py          # Orchestrator (main entry point)
│   │   ├── alert_models.py      # Alert, FeedbackRecord (Pydantic)
│   │   └── result_models.py     # AnalysisResult, Verdict (Pydantic)
│   ├── models/
│   │   ├── model_router.py      # Backend selection + fallback chain
│   │   ├── claude_backend.py    # Anthropic SDK + prompt caching
│   │   └── gpt_backend.py       # OpenAI SDK + JSON-object mode
│   ├── integrations/
│   │   ├── siem_base.py
│   │   ├── splunk_connector.py
│   │   ├── elk_connector.py
│   │   └── webhook_output.py
│   ├── feedback/
│   │   ├── feedback_system.py   # SQLite, auto-recovers from corrupt DB
│   │   ├── learning_engine.py   # Aggregate SQL → RuleInsight
│   │   └── prompt_enhancer.py   # Cached system-prompt augmentation
│   ├── reporting/
│   │   ├── metrics.py
│   │   └── report_generator.py
│   ├── anonymize.py
│   ├── cost_controller.py       # Atomic writes, daily/monthly rollover
│   ├── retry.py                 # Async exponential backoff + rate-limit handling
│   └── logger.py                # structlog setup
├── scripts/
│   ├── init_client.py
│   ├── run_client.py
│   └── migrate_config.py
├── tests/
└── docs/
```

## Alert lifecycle

1. **Ingestion** — `SIEMConnector.fetch_alerts()` pulls open alerts and normalises them into `Alert` objects.
2. **Budget check** — `CostController.check_limit()` reads `data/<client_id>/cost.json`; raises `CostLimitExceededError` if the daily or monthly cap is exhausted.
3. **Anonymisation** — `Anonymizer.anonymize()` redacts IPs, usernames, hostnames, emails, card numbers, SSNs, and API keys using a per-client salted hash. The reverse map stays in memory.
4. **Prompt enhancement** — `PromptEnhancer.enhance()` returns either the base system prompt or the base prompt with cached client-specific hints prepended. Hints survive `cache_ttl_sec` (5 min by default) so Anthropic prompt caching keeps hitting.
5. **Routing + retry** — `ModelRouter.route()` selects the primary backend; on any non-auth failure (`TransientBackendError`, `RateLimitedError`) the `src/retry.py` helper retries with exponential backoff. After exhausting attempts, the next backend in `model_fallbacks` is tried.
6. **Cost accounting** — `CostController.record()` atomically writes the updated spend.
7. **Output** — Results go back to the SIEM and optionally to a webhook. Analysts mark them right or wrong; verdicts feed back into `FeedbackSystem`.

## Feedback loop

See [FEEDBACK.md](FEEDBACK.md) for the full story. Summary:

* `FeedbackSystem` stores per-client analyst decisions in SQLite. `rule_name` is a real column with an index so aggregations don't deserialise JSON.
* `LearningEngine` runs an aggregate SQL query (`rule_stats()`), surfaces rules with high FP rates and enough samples, and converts each into a natural-language hint.
* `PromptEnhancer` memoises the hints per client with a TTL; the analyzer prepends them to every system prompt.
* No fine-tuning, no embeddings. Works with any backend.

## Multi-tenancy

Each client has:

* `config/client_configs/<id>.yaml` — model, fallbacks, SIEM, cost limits, optional `anonymize_salt` and `learning` overrides.
* `data/<id>/feedback.db` and `data/<id>/cost.json` — isolated state.
* `ALERTTRIAGE_<CLIENT_ID_UPPER>_API_KEY` — SIEM credential from the environment.

Clients share the process and the AI provider credentials, but no data
crosses client boundaries.

## Adding a new SIEM connector

1. Create `src/integrations/<name>_connector.py`, subclass `SIEMConnector`.
2. Implement `fetch_alerts()`, `send_result()`, and `health_check()`. Optionally `close()` to release HTTP sessions.
3. Register it in `scripts/run_client.py`'s `_build_connector()` factory.
4. Add a `type: <name>` example to `config/client_configs/template.yaml`.

## Adding a new AI model

1. Add a spec to `config/models.yaml` (`provider`, `model_name`, `max_tokens`, `temperature`).
2. If it's a new provider, create `src/models/<provider>_backend.py` with `async def analyze(alert, *, system_prompt, retry_config) -> AnalysisResult`.
3. Register it in `ModelRouter._load_backends()`.
4. If pricing matters, add an entry to the backend's `_PRICING` dict.
