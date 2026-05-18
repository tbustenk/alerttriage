# AlertTriage v2 — Architecture

## System overview

AlertTriage v2 is a multi-tenant AI alert triage platform. It ingests security alerts from SIEM systems, anonymizes PII, routes them to a configurable AI model, and writes verdicts back to the SIEM (or a webhook). Analyst feedback is captured and fed back into the system to improve future accuracy.

```
SIEM (Splunk / ELK / Webhook)
        │
        ▼
  ┌─────────────────────────────────────────────────────────┐
  │                   AlertAnalyzer                         │
  │                                                         │
  │  1. CostController.check_limit()                        │
  │  2. Anonymizer.anonymize()   ─── strips PII             │
  │  3. ModelRouter.route()      ─── selects AI backend     │
  │     ├─ ClaudeBackend  (Anthropic SDK, prompt caching)   │
  │     └─ GPTBackend     (OpenAI SDK, JSON mode)           │
  │  4. CostController.record()  ─── accounting             │
  └─────────────────────────────────────────────────────────┘
        │
        ▼
  AnalysisResult (verdict + confidence + reasoning + cost)
        │
        ├──▶ SIEM connector  (write verdict back)
        ├──▶ WebhookOutput   (SOAR / ticketing)
        └──▶ FeedbackSystem  (store for learning)
```

---

## How alerts flow through the system

1. **Ingestion** — `SIEMConnector.fetch_alerts()` pulls open/new alerts from the SIEM and normalises them into `Alert` objects (common schema regardless of source).

2. **Budget check** — `CostController.check_limit()` reads the client's daily and monthly spend state. If a limit is exceeded, an exception is raised before any AI call is made.

3. **Anonymization** — `Anonymizer.anonymize()` scans all string fields for PII (IPs, usernames, credit card numbers, SSNs, API keys) and replaces them with stable pseudonyms (`<IP:a3f2b1c4>`). A `reverse_map` is kept in memory so results can be de-anonymised for display, but it is never persisted or sent to the AI.

4. **Model routing** — `ModelRouter.route()` reads the client's `model` and `model_fallbacks` config keys, selects the appropriate backend, and calls `backend.analyze(alert)`. If the primary backend fails, it tries each fallback in order.

5. **AI analysis** — The backend (Claude or GPT) constructs a structured prompt, calls the API, parses the JSON response, and returns an `AnalysisResult` with verdict, confidence, reasoning, risk factors, and cost metadata.

6. **Cost accounting** — `CostController.record()` persists the cost to `data/<client_id>/cost.json`.

7. **Output** — The result is written back to the SIEM (`SIEMConnector.send_result()`) and optionally to a webhook. The raw result is also passed to `FeedbackSystem` so analysts can later mark it right or wrong.

---

## How feedback improves analysis

```
Analyst marks alert → FeedbackSystem.record()
                              │
                              ▼
                       SQLite (data/<client>/feedback.db)
                              │
                              ▼
                    LearningEngine.generate_prompt_hints()
                              │
                    ┌─────────┴────────────────┐
                    │  Injected into system     │
                    │  prompt on next analysis  │
                    └───────────────────────────┘
```

`LearningEngine` reads all feedback for a client and identifies detection rules with consistently high false-positive rates (>50% FP, ≥5 samples). It generates a concise block of natural-language hints that is prepended to the AI system prompt, steering the model toward more conservative verdicts for those rules.

The loop is intentionally simple — no fine-tuning, no embeddings, just prompt engineering from real analyst decisions. This means it works with any model backend and requires no ML infrastructure.

---

## How to add a new SIEM connector

1. Create `alerttriage/src/integrations/<name>_connector.py`.
2. Subclass `SIEMConnector` from `siem_base.py`.
3. Implement `fetch_alerts()`, `send_result()`, and `health_check()`.
4. Register it in `scripts/run_client.py`'s `_build_connector()` factory.
5. Add a `type: <name>` example to `config/client_configs/template.yaml`.

The `Alert` model is the canonical interchange format — your connector only needs to map SIEM-native fields into it. See `SplunkConnector._parse_notable()` and `ELKConnector._parse_signal()` for reference.

---

## How to switch AI models

**Per-client:** edit `config/client_configs/<client_id>.yaml`:
```yaml
model: claude-opus        # primary
model_fallbacks:
  - gpt-4o-mini           # tried if claude-opus fails
```

**Add a new model:**
1. Add an entry to `config/models.yaml` with `provider`, `model_name`, `max_tokens`, `temperature`.
2. If it's a new provider, create a backend file in `src/models/` implementing `async def analyze(alert) -> AnalysisResult`.
3. Register it in `ModelRouter._load_backends()`.

---

## How to onboard a new client

**Interactive (recommended):**
```bash
python scripts/init_client.py
```

**Manual:**
1. Copy `config/client_configs/template.yaml` → `config/client_configs/<client_id>.yaml`.
2. Fill in `client_id`, `siem`, `model`, `cost_limits`.
3. Set SIEM credentials via env var: `ALERTTRIAGE_<CLIENT_ID_UPPER>_API_KEY`.
4. Run a health check: `python scripts/run_client.py --client-id <id> --limit 1 --dry-run`.

---

## Multi-tenancy and isolation

Each client has:
- Its own config file (`config/client_configs/<id>.yaml`)
- Its own data directory (`data/<id>/`)
  - `feedback.db` — analyst feedback (SQLite)
  - `cost.json` — running spend totals
- Its own model selection and fallback chain
- Its own cost limits (enforced before every AI call)
- Its own anonymization tokens (salted per-client if `anonymize_salt` differs)

Clients share the process and the AI backend credentials, but no data crosses client boundaries.

---

## Directory reference

```
alerttriage/
├── config/
│   ├── config_manager.py      # Config loading and validation
│   ├── default_config.yaml    # System-wide defaults
│   ├── models.yaml            # AI model registry
│   └── client_configs/        # One YAML per client
├── src/
│   ├── core/
│   │   ├── analyzer.py        # Orchestrator (main entry point)
│   │   ├── alert_models.py    # Alert, FeedbackRecord (Pydantic)
│   │   └── result_models.py   # AnalysisResult, Verdict (Pydantic)
│   ├── models/
│   │   ├── model_router.py    # Backend selection + fallback
│   │   ├── claude_backend.py  # Anthropic SDK integration
│   │   └── gpt_backend.py     # OpenAI SDK integration
│   ├── integrations/
│   │   ├── siem_base.py       # Abstract base class
│   │   ├── splunk_connector.py
│   │   ├── elk_connector.py
│   │   └── webhook_output.py
│   ├── feedback/
│   │   ├── feedback_system.py # SQLite store
│   │   └── learning_engine.py # Prompt hint generation
│   ├── reporting/
│   │   ├── metrics.py         # Accuracy / cost calculations
│   │   └── report_generator.py# HTML + JSON report output
│   ├── anonymize.py           # PII pseudonymisation
│   ├── cost_controller.py     # Per-client budget enforcement
│   └── logger.py              # structlog setup
├── scripts/
│   ├── init_client.py         # New client wizard
│   ├── run_client.py          # Main run loop
│   └── migrate_config.py      # Config version migration
├── tests/                     # pytest unit + integration tests
├── data/                      # Runtime state (git-ignored)
└── reports/                   # Generated reports (git-ignored)
```
