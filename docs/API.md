# AlertTriage v2 — Internal API Reference

## Core classes

### `AlertAnalyzer`
`alerttriage.src.core.analyzer`

The top-level orchestrator. Create one per process (or per client for full isolation).

```python
from alerttriage.config.config_manager import ConfigManager
from alerttriage.src.core.analyzer import AlertAnalyzer

config = ConfigManager("config", client_id="acme-corp")
analyzer = AlertAnalyzer(config)

result = await analyzer.analyze(alert)
results = await analyzer.analyze_batch(alerts, max_concurrency=10)
```

| Method | Signature | Description |
|---|---|---|
| `analyze` | `(alert, *, dry_run=False) -> AnalysisResult` | Analyse one alert. Raises `CostLimitExceededError`, `ModelUnavailableError`. |
| `analyze_batch` | `(alerts, *, max_concurrency=5) -> list[AnalysisResult]` | Bounded-concurrency batch analysis. |

---

### `Alert`
`alerttriage.src.core.alert_models`

Pydantic model. All SIEM connectors produce `Alert` objects.

| Field | Type | Description |
|---|---|---|
| `id` | `str` | UUID, auto-generated. |
| `client_id` | `str` | Slug of the owning client. |
| `source` | `AlertSource` | Enum: `splunk`, `elk`, `webhook`, `manual`. |
| `rule_name` | `str` | Detection rule that fired. |
| `severity` | `AlertSeverity` | Enum: `critical`, `high`, `medium`, `low`, `info`. |
| `title` | `str` | Human-readable alert title. |
| `description` | `str` | Alert body / rule description. |
| `raw_payload` | `dict` | Full SIEM event (stripped of PII before AI call). |
| `context` | `AlertContext` | Structured enrichment: host, user, network, threat intel. |
| `tags` | `list[str]` | MITRE ATT&CK tags or custom labels. |
| `timestamp` | `datetime` | Alert creation time (UTC). |

---

### `AnalysisResult`
`alerttriage.src.core.result_models`

| Field | Type | Description |
|---|---|---|
| `id` | `str` | UUID. |
| `alert_id` | `str` | Back-reference to the source `Alert.id`. |
| `verdict` | `Verdict` | `true_positive`, `false_positive`, `needs_escalation`, `needs_investigation`, `benign`, `unknown`. |
| `confidence` | `float` | 0.0–1.0. |
| `summary` | `str` | One-sentence verdict summary. |
| `reasoning` | `str` | Full AI reasoning text. |
| `risk_factors` | `list[RiskFactor]` | Named risk indicators with weights. |
| `recommended_actions` | `list[RecommendedAction]` | Ordered action items. |
| `prompt_tokens` | `int` | Input tokens billed. |
| `completion_tokens` | `int` | Output tokens billed. |
| `cost_usd` | `float` | Estimated API cost. |
| `latency_ms` | `int` | Wall-clock time for the AI call. |

---

### `ModelRouter`
`alerttriage.src.models.model_router`

```python
router = ModelRouter(config)
result = await router.route(alert)   # raises ModelUnavailableError if all backends fail
```

Reads `client_cfg["model"]` and `client_cfg["model_fallbacks"]` from config.

---

### `FeedbackSystem`
`alerttriage.src.feedback.feedback_system`

```python
fs = FeedbackSystem(data_dir=Path("data"), client_id="acme-corp")
fs.record(feedback_record)
records = fs.get_for_client(limit=500, offset=0)
accuracy = fs.accuracy()   # float 0–1
```

SQLite-backed. Thread-safe.

---

### `LearningEngine`
`alerttriage.src.feedback.learning_engine`

```python
engine = LearningEngine(feedback_system)
hints = engine.generate_prompt_hints(max_hints=10)   # str, inject into system prompt
insights = engine.compute_rule_insights()            # list[RuleInsight]
worst = engine.worst_performing_rules(n=5)
```

---

### `ConfigManager`
`alerttriage.config.config_manager`

```python
config = ConfigManager("config")
config.get_client("acme-corp")   # dict merged from defaults + client YAML + env vars
config.list_clients()            # list of client IDs with config files
config.default_model_id          # str
config.data_dir                  # Path
```

---

### `Anonymizer`
`alerttriage.src.anonymize`

```python
anonymizer = Anonymizer(config)
anon_alert, reverse_map = anonymizer.anonymize(alert)
# reverse_map: {"<IP:a3f2b1c4>": "10.0.0.1", ...}
```

The reverse map is intentionally in-memory only. Do not persist or log it.

---

### `CostController`
`alerttriage.src.cost_controller`

```python
cc = CostController(config)
cc.check_limit("acme-corp")           # raises CostLimitExceededError if over budget
cc.record("acme-corp", 0.0023)        # add spend
cc.get_spend("acme-corp")            # {"daily_usd": ..., "monthly_usd": ..., "total_usd": ...}
cc.reset_daily("acme-corp")
```

---

## SIEM connector interface

All connectors implement `SIEMConnector` (ABC):

```python
async def fetch_alerts(*, limit=100, since_id=None) -> list[Alert]: ...
async def send_result(result: AnalysisResult) -> bool: ...
async def health_check() -> bool: ...
# optional:
async def enrich_alert(alert: Alert) -> Alert: ...
async def acknowledge_alert(alert_id: str) -> bool: ...
```

---

## Exceptions

| Exception | Module | When raised |
|---|---|---|
| `CostLimitExceededError` | `cost_controller` | Client over daily or monthly budget. |
| `ModelUnavailableError` | `model_router` | All configured backends failed. |
