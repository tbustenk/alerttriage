# AlertTriage v2 — Client Setup Guide

## Prerequisites

- Python 3.11+
- Access credentials for the client's SIEM (Splunk token or ELK API key)
- An Anthropic API key (and/or OpenAI key if using GPT backends)

## Quick start (new client)

### 1. Install AlertTriage

```bash
pip install -r requirements.txt
# or, for editable development install:
pip install -e .
```

### 2. Configure environment variables

```bash
cp .env.example .env
# Edit .env — fill in ANTHROPIC_API_KEY and the client's SIEM credential
```

### 3. Run the setup wizard

```bash
python scripts/init_client.py
```

The wizard asks for:
- Client name / ID
- SIEM type and connection details
- AI model preference
- Cost limits

It writes `config/client_configs/<client_id>.yaml` and creates `data/<client_id>/`.

### 4. Verify connectivity

```bash
python scripts/run_client.py --client-id <client_id> --limit 1 --dry-run
```

`--dry-run` fetches one alert from the SIEM but does not call the AI or write results back.
Remove `--dry-run` once you're confident the connection is working.

### 5. Run a full triage batch

```bash
python scripts/run_client.py --client-id <client_id> --limit 100
```

---

## Manual client config

Copy the template and fill in the blanks:

```bash
cp config/client_configs/template.yaml config/client_configs/acme-corp.yaml
```

Key fields:

| Field | Description |
|---|---|
| `client_id` | Lowercase slug. Must match the filename. |
| `model` | Key from `config/models.yaml`. Default: `claude-sonnet`. |
| `siem.type` | `splunk`, `elk`, `webhook`, or `none`. |
| `cost_limits.daily_usd` | Hard stop per day. `null` = no limit. |
| `anonymize` | Always `true` for production. |

---

## Rotating SIEM credentials

Credentials live in environment variables, not in YAML files.

Pattern: `ALERTTRIAGE_<CLIENT_ID_UPPERCASE>_API_KEY`

For client `acme-corp`:
```bash
export ALERTTRIAGE_ACME_CORP_API_KEY=new-token-here
```

No config file edits needed.

---

## Capturing analyst feedback

After an analyst closes an alert, record their decision:

```python
from alerttriage.src.core.alert_models import FeedbackRecord
from alerttriage.src.feedback.feedback_system import FeedbackSystem
from pathlib import Path

fs = FeedbackSystem(Path("data"), client_id="acme-corp")
fs.record(FeedbackRecord(
    alert_id="<original-alert-id>",
    analysis_id="<analysis-result-id>",
    client_id="acme-corp",
    analyst_id="jsmith",
    analyst_verdict="false_positive",   # or true_positive / escalated / closed
    analyst_notes="Triggered by vuln scanner.",
    ai_verdict_was_correct=False,
))
```

Feedback automatically influences the AI's system prompt for that client on future runs
(via `LearningEngine.generate_prompt_hints()`).

---

## Generating reports

```python
from alerttriage.src.reporting.report_generator import ReportGenerator
from alerttriage.src.reporting.metrics import compute_client_metrics
from pathlib import Path

# results: list[AnalysisResult] from your last run
# feedback: list[FeedbackRecord] from FeedbackSystem.get_for_client()
metrics = compute_client_metrics("acme-corp", results, feedback)

rg = ReportGenerator(Path("reports"))
html_path = rg.generate_html(metrics)
json_path = rg.generate_json(metrics)
print(f"Report: {html_path}")
```

---

## Switching AI models mid-deployment

Edit `config/client_configs/<client_id>.yaml`:

```yaml
model: claude-opus          # was claude-sonnet
model_fallbacks:
  - claude-sonnet           # fallback if opus fails
```

No restart required if you rebuild the `ModelRouter` (it reads config on init).
For long-running services, trigger a config reload or restart the process.

---

## Cost management

Spend is tracked in `data/<client_id>/cost.json`. To reset the daily counter (e.g., from a cron job at midnight):

```python
from alerttriage.src.cost_controller import CostController
# config = your ConfigManager instance
cc = CostController(config)
cc.reset_daily("acme-corp")
```

To check current spend:

```python
print(cc.get_spend("acme-corp"))
# {'daily_usd': 3.42, 'monthly_usd': 41.07, 'total_usd': 189.33}
```
