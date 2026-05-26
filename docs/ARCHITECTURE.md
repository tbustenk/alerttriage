# AlertTriage v2 — Architecture Guide

## Table of Contents

- [System Overview](#system-overview)
- [Design Goals](#design-goals)
- [Component Diagram](#component-diagram)
- [Component Reference](#component-reference)
- [Data Flow](#data-flow)
- [Database Schema](#database-schema)
- [API Authentication Flow](#api-authentication-flow)
- [Config Resolution Order](#config-resolution-order)
- [Webhook Delivery Pipeline](#webhook-delivery-pipeline)
- [Analytics Computation Pipeline](#analytics-computation-pipeline)
- [On-Disk Layout](#on-disk-layout)
- [Extending the System](#extending-the-system)

---

## System Overview

AlertTriage v2 is a Python 3.12 service that routes security alerts through an AI triage pipeline, records analyst feedback, and uses that feedback to continuously improve triage accuracy. It exposes a FastAPI REST API, a Flask web dashboard, and a one-shot CLI runner for SIEM batch jobs.

The system is deliberately simple at the core — no message broker, no distributed state, no external cache. Each component is a plain Python class with a narrow interface, making the system easy to test and extend without infrastructure dependencies.

---

## Design Goals

| Goal | How it is achieved |
|------|--------------------|
| **No single point of failure** | All state persisted to local files (SQLite, JSON). The API restarts cleanly with no data loss. |
| **Minimal external dependencies** | SQLite for feedback storage, in-memory LRU for result cache, in-memory sliding window for rate limiting. Redis, Celery, and message brokers are not required. |
| **Per-client isolation** | Every client gets its own feedback database, cost state file, and config YAML. There is no shared mutable state between clients. |
| **Accuracy that improves over time** | The LearningEngine mines analyst feedback and injects prompt hints into the next analysis, closing the human-in-the-loop. |
| **Observable** | Prometheus metrics, structured JSON logs, health endpoint, and full dashboard endpoint are all built-in. |
| **Safe AI integration** | PII is pseudonymised before any data leaves the system. Cost limits prevent runaway AI spend. |

---

## Component Diagram

```
┌───────────────────────────────────────────────────────────────────────┐
│  Clients / Integrations                                               │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────────┐            │
│  │  SIEM/SOAR  │  │  REST caller │  │  Flask Dashboard  │            │
│  │  (Splunk,   │  │  (curl, SDK, │  │  (browser UI)     │            │
│  │   ELK, etc) │  │   SOAR)      │  │                   │            │
│  └──────┬──────┘  └──────┬───────┘  └────────┬──────────┘            │
└─────────│────────────────│───────────────────│───────────────────────┘
          │                │                   │
          │ CLI runner      │ HTTP              │ HTTP (dashboard/*)
          ▼                ▼                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│  FastAPI Application  (src/api/app.py)                              │
│                                                                     │
│  Middleware: CORS · Rate Limiter · Security Headers                 │
│                                                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────────┐ │
│  │  Core Routes │  │  Extension   │  │  Operations               │ │
│  │              │  │  Routers     │  │                           │ │
│  │  POST/analyze│  │  /analytics/*│  │  GET /health              │ │
│  │  GET /results│  │  /webhooks/* │  │  GET /metrics (Prometheus)│ │
│  │  POST/feedback│  │  /config/*  │  │  GET /dashboard/health    │ │
│  │  GET /stats  │  │              │  │                           │ │
│  │  GET /hints  │  │              │  │                           │ │
│  │  POST/context│  │              │  │                           │ │
│  └──────┬───────┘  └──────┬───────┘  └──────────┬────────────────┘ │
└─────────│─────────────────│──────────────────────│──────────────────┘
          │                 │                      │
          ▼                 │                      ▼
┌─────────────────────┐     │             ┌─────────────────┐
│  AlertAnalyzer      │     │             │  HealthMonitor  │
│  (src/core/         │     │             │  (src/monitoring│
│   analyzer.py)      │     │             │   /health_      │
│                     │     │             │   monitor.py)   │
│  1. CostController  │     │             └─────────────────┘
│  2. Anonymizer      │     │
│  3. PromptEnhancer  │     ▼
│  4. ModelRouter     │  ┌─────────────────────────────────────────┐
└────────┬────────────┘  │  Extension Engines                      │
         │               │  ┌───────────────┐  ┌────────────────┐  │
         │               │  │ AnalyticsEngine│  │ WebhookManager │  │
         │               │  │ (ROI, FP,      │  │ (delivery,     │  │
         ▼               │  │  reports)      │  │  HMAC signing) │  │
┌─────────────────────┐  │  └───────────────┘  └────────────────┘  │
│  AI Backends        │  │  ┌───────────────┐  ┌────────────────┐  │
│  (src/models/)      │  │  │ConfigVersioning│  │FeatureFlags    │  │
│                     │  │  │(snapshots,     │  │(hot-reload)    │  │
│  claude_backend.py  │  │  │ rollback)      │  │                │  │
│  gpt_backend.py     │  │  └───────────────┘  └────────────────┘  │
│  model_router.py    │  └─────────────────────────────────────────┘
└─────────────────────┘
         │
         ▼
┌────────────────────────────────────────┐
│  Persistence Layer                     │
│                                        │
│  data/<client_id>/feedback.db  SQLite  │
│  data/<client_id>/cost.json    JSON    │
│  data/webhooks.json            JSON    │
│  data/versions/<client>/       JSON    │
│  config/client_configs/*.yaml  YAML    │
└────────────────────────────────────────┘
```

---

## Component Reference

### ConfigManager (`config/config_manager.py`)

Loads and validates all YAML configuration. Implements the three-level resolution order (default → env vars → client). All values are passed through Pydantic models, so a malformed config raises a `ValidationError` at startup rather than at first use.

Exposes typed accessors: `get_client()`, `get_learning()`, `list_clients()`, and properties for `retry`, `concurrency`, and `learning` defaults.

### AlertAnalyzer (`src/core/analyzer.py`)

The single entry point for alert triage. Orchestrates the four-step pipeline in order:

1. **CostController.check_limit** — aborts if the client has exceeded its daily or monthly budget.
2. **Anonymizer.anonymize** — replaces IPs, emails, credit card numbers, API keys, and SSNs with stable pseudonyms before any data leaves the system.
3. **PromptEnhancer.enhance** — prepends learned false-positive hints to the system prompt.
4. **ModelRouter.route** — selects the primary model (or falls back), retries with exponential backoff, and returns an `AnalysisResult`.

Also exposes `analyze_batch()` for bounded-concurrency bulk triage.

### FeedbackSystem (`src/feedback/feedback_system.py`)

SQLite-backed store of analyst verdicts. One database file per client at `data/<client_id>/feedback.db`. Designed for fast aggregation:

- `rule_name` is a real column with an index — the learning engine never has to deserialise JSON to aggregate.
- Writes are batched in a single transaction to support bulk SOAR imports.
- Corrupt databases are moved aside (not deleted) and a fresh empty database is initialised so the service keeps running.

### LearningEngine (`src/feedback/learning_engine.py`)

Mines the feedback database to find rules where the AI consistently fires incorrectly. A rule earns a prompt hint when its false-positive rate exceeds `fp_rate_threshold` and it has at least `min_sample_size` feedback rows. Hints are natural-language sentences prepended to the Claude system prompt on the next analysis call. No fine-tuning or embeddings required.

### CostController (`src/cost_controller.py`)

Persists daily and monthly AI spend to `data/<client_id>/cost.json`. Daily and monthly totals reset automatically on wall-clock boundary — no cron job required. Raises `CostLimitExceededError` before calling the AI if a limit would be exceeded. Writes are atomic (write + rename) to prevent corrupt state on crash.

### HealthMonitor (`src/monitoring/health_monitor.py`)

Tracks three signal types in rolling in-memory windows:
- **Latency** — p50/p95/p99 across all requests.
- **Accuracy** — per-client AI vs. analyst agreement rate.
- **Component health** — database connectivity, feedback system availability.

Exposes three output formats: structured `HealthStatus`, a full JSON dashboard dict, and Prometheus text exposition.

### WebhookManager (`src/webhooks/manager.py`)

Stores webhook registrations in `data/webhooks.json`. When the API emits an event (e.g., after analysis or feedback), the manager delivers it asynchronously to all matching registered endpoints with HMAC-SHA256 signing and exponential-backoff retry.

### AnalyticsEngine (`src/analytics/engine.py`)

Computes ROI, per-rule breakdown, false-positive pattern analysis, accuracy trends, and weekly summaries from the feedback database. Reports are exportable as JSON, HTML, PDF (requires `fpdf2`), or CSV.

---

## Data Flow

### Alert Triage Flow

```
POST /analyze
    │
    ▼
Rate limiter check (sliding-window per IP, 60 req/min default)
    │
    ▼
Build Alert model (validate severity, source, client_id)
    │
    ▼
AlertAnalyzer.analyze(alert)
    │
    ├─► CostController.check_limit()
    │       If daily_usd or monthly_usd exceeded → raise 402
    │
    ├─► Anonymizer.anonymize(alert)
    │       IPs, emails, SSNs, API keys → stable pseudonyms
    │       Reverse map held in memory only, never persisted
    │
    ├─► PromptEnhancer.enhance(system_prompt)
    │       Query FeedbackSystem for this client
    │       LearningEngine generates hints (cache TTL: 300s)
    │       Hints prepended to system prompt
    │
    └─► ModelRouter.route(anon_alert, system_prompt)
            Select primary model from client config
            Call AI backend (Claude or GPT)
            On 5xx or timeout: retry with exponential backoff
            On primary failure: try model_fallbacks list
            Return AnalysisResult
    │
    ▼
CostController.record(client_id, cost_usd)
    │
    ▼
Cache result in _result_store (LRU, max 10,000 entries)
    │
    ▼
WebhookManager.emit(alert_analyzed event)
    │
    ▼
Return AnalysisResponse to caller
```

### Feedback → Learning Loop

```
POST /feedback
    │
    ▼
Build FeedbackRecord (validate analyst_verdict)
    │
    ▼
FeedbackSystem.record(record)
    │   Upserts to data/<client_id>/feedback.db
    │   Indexed by client_id, rule_name, verdict, timestamp
    │
    ▼
AlertAnalyzer.invalidate_prompt_cache(client_id)
    │   Forces LearningEngine to rebuild hints on next analysis
    │
    ▼
WebhookManager.emit(feedback_recorded event)
    │
    ▼
Return {"status": "recorded", "id": ...}

--- Next POST /analyze for this client ---

PromptEnhancer.enhance()
    │
    └─► LearningEngine.generate_prompt_hints()
            SELECT rule_name, COUNT(*), SUM(ai_correct), SUM(fp)
            FROM feedback WHERE client_id=?
            GROUP BY rule_name
            Rules with fp_rate >= 0.50 AND total >= 5 earn hints
            E.g.: "port_scan_detected triggers 70% FP rate.
                   Consider benign before flagging."
```

---

## Database Schema

### `data/<client_id>/feedback.db`

```sql
CREATE TABLE feedback (
    id              TEXT PRIMARY KEY,       -- UUID
    alert_id        TEXT NOT NULL,          -- Original alert identifier
    analysis_id     TEXT NOT NULL,          -- AnalysisResult.id
    client_id       TEXT NOT NULL,          -- Owning client
    analyst_id      TEXT NOT NULL,          -- Who made the call (email/username)
    analyst_verdict TEXT NOT NULL,          -- true_positive | false_positive |
                                            --   escalated | closed
    analyst_notes   TEXT DEFAULT '',        -- Free-text explanation
    ai_correct      INTEGER NOT NULL,       -- 1 if AI verdict matched analyst
    rule_name       TEXT NOT NULL           -- Detection rule name (indexed)
                    DEFAULT 'unknown',
    timestamp       TEXT NOT NULL,          -- ISO-8601 UTC
    metadata        TEXT DEFAULT '{}'       -- JSON blob for extensions
);

CREATE INDEX idx_feedback_client    ON feedback(client_id);
CREATE INDEX idx_feedback_alert     ON feedback(alert_id);
CREATE INDEX idx_feedback_rule      ON feedback(client_id, rule_name);
CREATE INDEX idx_feedback_verdict   ON feedback(client_id, analyst_verdict);
CREATE INDEX idx_feedback_timestamp ON feedback(client_id, timestamp DESC);
```

SQLite WAL mode is used for concurrent read access. Schema version is tracked via `PRAGMA user_version = 2`.

### `data/<client_id>/cost.json`

```json
{
  "daily_usd": 1.42,
  "monthly_usd": 18.35,
  "day": "2026-05-25",
  "month": "2026-05"
}
```

Daily and monthly totals reset automatically when the current wall-clock day or month differs from the stored value.

### `data/webhooks.json`

```json
[
  {
    "id": "uuid",
    "client_id": "acme-corp",
    "url": "https://hooks.example.com/alerttriage",
    "events": ["alert_analyzed", "threshold_exceeded"],
    "secret": "...",
    "active": true,
    "consecutive_failures": 0,
    "last_success_at": "2026-05-25T09:00:00Z",
    "last_failure_at": null,
    "created_at": "2026-05-01T00:00:00Z"
  }
]
```

---

## API Authentication Flow

```
Client sends: GET /stats/acme-corp
              X-API-Key: my-secret-key-abc

FastAPI dependency: require_api_key()
    │
    ├─ VALID_KEYS is empty (ALERTTRIAGE_API_KEYS not set)?
    │       → return "anonymous"  [open-access mode, dev only]
    │
    ├─ X-API-Key header missing?
    │       → 401 Unauthorized  {"detail": "Missing X-API-Key header"}
    │
    └─ secrets.compare_digest(provided, each valid key)
            Match found → return key (used in audit log)
            No match    → 403 Forbidden  {"detail": "Invalid API key"}
```

Keys are loaded once from `ALERTTRIAGE_API_KEYS` (comma-separated) at process start. A restart is required to add or remove keys. Comparisons use `secrets.compare_digest` to prevent timing attacks.

`GET /health` and `GET /metrics` are intentionally unauthenticated so load balancers and Prometheus can probe them without credentials.

---

## Config Resolution Order

Configuration is merged from three sources, applied in order (each layer overrides the previous):

```
1. config/default_config.yaml         ← System-wide defaults
        ↓ overridden by
2. Environment variables               ← ALERTTRIAGE_LOG_LEVEL, ALERTTRIAGE_DATA_DIR, etc.
        ↓ overridden by
3. config/client_configs/<id>.yaml    ← Per-client model, cost limits, learning thresholds
```

**Feature flags** follow the same pattern but via `config/features.yaml` and the per-client `features:` block. Environment variables of the form `ALERTTRIAGE_FEATURE_<FLAG>=true` override at the env layer.

**Alert-type settings** resolve in order: global `config/alert_types.yaml` defaults → global rule-specific entry → global fnmatch pattern → client config `alert_types:` block.

All layers pass through Pydantic validation. An invalid value fails at startup, not at first use.

---

## Webhook Delivery Pipeline

```
Event emitted (e.g., after POST /analyze returns):
    │
    ▼
WebhookManager.emit(event)
    │   Find all active registrations for event.client_id
    │   Filter by event.event_type ∈ registration.events
    │
    ▼
For each matching registration:
    delivery.deliver(registration, event)  [async, fire-and-forget]
        │
        ├─► Serialise event to JSON bytes
        │
        ├─► Build headers:
        │       Content-Type: application/json
        │       X-AlertTriage-Event: alert_analyzed
        │       X-AlertTriage-Event-Id: <uuid>
        │       X-AlertTriage-Timestamp: <ISO-8601>
        │       X-AlertTriage-Client: acme-corp
        │       X-AlertTriage-Signature: sha256=<hmac> (if secret set)
        │
        ├─► HMAC-SHA256 signing:
        │       sig = hmac(secret.encode(), payload_bytes, sha256).hexdigest()
        │       header = f"sha256={sig}"
        │
        └─► HTTP POST with exponential-backoff retry
                Attempt 1: immediate
                Attempt 2: 1s delay
                Attempt 3: 2s delay
                Attempt 4: 4s delay (capped at 30s)
                4xx responses: NOT retried (client error)
                5xx / network: retried up to max_attempts (default 4)

On success: registration.consecutive_failures = 0, last_success_at updated
On failure: registration.consecutive_failures += 1, last_failure_at updated
```

Receivers should verify the HMAC signature before processing payloads:

```python
import hashlib, hmac

def verify(payload_bytes: bytes, secret: str, header: str) -> bool:
    expected = "sha256=" + hmac.new(
        secret.encode(), payload_bytes, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(header, expected)
```

---

## Analytics Computation Pipeline

```
GET /analytics/roi/acme-corp?days=30

    ▼
AnalyticsEngine.compute_roi(client_id, days=30, ...)
    │
    ├─► FeedbackSystem.rule_stats()
    │       SELECT rule_name, COUNT(*), SUM(ai_correct), SUM(fp)
    │       FROM feedback WHERE client_id=? AND timestamp >= ?
    │       GROUP BY rule_name
    │
    ├─► Compute:
    │       total_alerts = SUM(all rule totals)
    │       hours_saved  = total_alerts * analyst_minutes_per_alert / 60
    │       labor_value  = hours_saved * analyst_hourly_rate
    │       ai_cost      = SUM(cost from cost.json over period)
    │       net_roi      = labor_value - ai_cost
    │       roi_pct      = (net_roi / ai_cost) * 100
    │
    └─► Return ROIResult dataclass

GET /analytics/report/acme-corp/download?format=pdf

    ▼
AnalyticsEngine.generate_client_report()
    │   Runs compute_roi + compute_weekly_summary +
    │   compute_alert_type_breakdown + compute_fp_analysis
    │
    ▼
PDFExporter(reports_dir).export(report)
    │   Generates PDF via fpdf2
    │   Saves to reports/<client_id>/<timestamp>.pdf
    │
    ▼
FileResponse — streamed to caller
```

Scheduled reports follow the same pipeline but are driven by `ReportScheduler.run()`, which wakes up every 60 seconds, checks each client's schedule, and emails the report via SMTP if the delivery window has arrived.

---

## On-Disk Layout

```
alerttriage/                     ← repo root (must be named "alerttriage")
├── config/
│   ├── config_manager.py        # Loads + validates YAML, exposes typed views
│   ├── default_config.yaml      # System-wide defaults
│   ├── models.yaml              # AI model registry
│   ├── features.yaml            # Global feature flags
│   ├── alert_types.yaml         # Per-rule confidence thresholds
│   └── client_configs/          # One YAML per client
├── src/
│   ├── api/
│   │   ├── app.py               # FastAPI app + lifespan
│   │   ├── auth.py              # X-API-Key dependency
│   │   ├── models.py            # Request/response Pydantic models
│   │   ├── rate_limit.py        # Sliding-window limiter
│   │   └── routers/
│   │       ├── analytics.py     # /analytics/* endpoints
│   │       ├── webhooks.py      # /webhooks/* endpoints
│   │       └── config_api.py    # /config/* endpoints
│   ├── core/
│   │   ├── analyzer.py          # Orchestrator
│   │   ├── alert_models.py      # Alert, FeedbackRecord
│   │   └── result_models.py     # AnalysisResult, Verdict
│   ├── models/
│   │   ├── model_router.py      # Backend selection + fallback chain
│   │   ├── claude_backend.py    # Anthropic SDK
│   │   └── gpt_backend.py       # OpenAI SDK
│   ├── feedback/
│   │   ├── feedback_system.py   # SQLite store, auto-recovery
│   │   ├── learning_engine.py   # Rule insight aggregation
│   │   └── prompt_enhancer.py   # TTL-cached prompt augmentation
│   ├── analytics/
│   │   ├── engine.py            # ROI / breakdown / trends
│   │   ├── exporters.py         # PDF, HTML, CSV, JSON
│   │   └── scheduler.py         # Automated scheduled reports
│   ├── webhooks/
│   │   ├── manager.py           # Registration store + emit
│   │   ├── delivery.py          # HMAC signing + retry
│   │   └── events.py            # Event types + payload models
│   ├── config_ext/
│   │   ├── versioning.py        # Config snapshots + rollback
│   │   ├── feature_flags.py     # Flag resolution
│   │   ├── alert_type_settings.py # Per-rule thresholds
│   │   └── hot_reload.py        # File watcher
│   ├── monitoring/
│   │   ├── health_monitor.py    # p95 latency, accuracy, Prometheus
│   │   └── notifier.py          # Slack / email alerting
│   ├── integrations/
│   │   ├── siem_base.py
│   │   ├── splunk_connector.py
│   │   └── elk_connector.py
│   ├── anonymize.py             # PII pseudonymisation
│   ├── cost_controller.py       # Spend tracking + limits
│   ├── retry.py                 # Exponential backoff helper
│   └── logger.py                # structlog configuration
├── dashboard/
│   ├── app.py                   # Flask dashboard app
│   └── templates/               # Jinja2 templates
├── data/                        # Runtime state (gitignored)
│   └── <client_id>/
│       ├── feedback.db
│       └── cost.json
├── reports/                     # Generated reports (gitignored)
├── scripts/
│   ├── run_api.py               # uvicorn launcher
│   └── run_client.py            # CLI batch runner
├── tests/
├── Dockerfile
└── docker-compose.yml
```

---

## Extending the System

### Adding a New SIEM Connector

1. Create `src/integrations/<name>_connector.py`, subclass `SIEMConnector`.
2. Implement `fetch_alerts()`, `send_result()`, and `health_check()`. Optionally `close()` to release HTTP sessions.
3. Register it in `scripts/run_client.py`'s `_build_connector()` factory.
4. Add a `type: <name>` example to `config/client_configs/template.yaml`.
5. Add tests using `pytest-httpx` or `unittest.mock` to mock HTTP calls.

### Adding a New AI Model Backend

1. Add a spec to `config/models.yaml` (`provider`, `model_name`, `max_tokens`, `temperature`).
2. If it's a new provider, create `src/models/<provider>_backend.py` with `async def analyze(alert, *, system_prompt, retry_config) -> AnalysisResult`.
3. Register it in `ModelRouter._load_backends()`.
4. Document pricing in the backend file via the `_PRICING` dict pattern.
5. Add an `[[tool.mypy.overrides]]` entry in `pyproject.toml` if the SDK lacks type stubs.
