# AlertTriage v2 — Client Onboarding Guide

This guide walks through setting up a new client from scratch: creating the config file, setting cost limits, configuring alert-type thresholds, testing with `dry_run`, setting up the first webhook, and viewing analytics.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Step 1: Create the Client Config YAML](#step-1-create-the-client-config-yaml)
- [Step 2: Set SIEM Credentials](#step-2-set-siem-credentials)
- [Step 3: Set Cost Limits](#step-3-set-cost-limits)
- [Step 4: Configure Alert-Type Thresholds](#step-4-configure-alert-type-thresholds)
- [Step 5: Test with dry_run](#step-5-test-with-dry_run)
- [Step 6: Run Your First Real Triage Batch](#step-6-run-your-first-real-triage-batch)
- [Step 7: Record Analyst Feedback](#step-7-record-analyst-feedback)
- [Step 8: Set Up Your First Webhook](#step-8-set-up-your-first-webhook)
- [Step 9: View Analytics](#step-9-view-analytics)
- [Ongoing Operations](#ongoing-operations)

---

## Prerequisites

- AlertTriage v2 is installed and the API is running (see [DEPLOYMENT.md](DEPLOYMENT.md))
- You have an `X-API-Key` value set in `ALERTTRIAGE_API_KEYS`
- You have `ANTHROPIC_API_KEY` set in the environment (or `.env`)
- For SIEM integration: Splunk REST API token or ELK API key for this client

---

## Step 1: Create the Client Config YAML

Create `config/client_configs/acme-corp.yaml`. The filename (without `.yaml`) is the client ID.

**Annotated example:**

```yaml
# config/client_configs/acme-corp.yaml

# Client identifier — must match the filename and be lowercase.
# Hyphens are allowed; the file must be named acme-corp.yaml.
client_id: acme-corp

# Human-readable name shown in the dashboard and reports.
display_name: "Acme Corporation SOC"

# AI model to use. Options: claude-sonnet, claude-haiku, claude-opus.
# claude-sonnet is the recommended default — best cost/accuracy balance.
model: claude-sonnet

# Fallback chain: if the primary model fails, these are tried in order.
# Remove this block to disable fallback (the API returns 500 on failure).
model_fallbacks:
  - claude-haiku

# SIEM integration — omit or set type: none for API-only usage.
siem:
  type: splunk                           # splunk | elk | none
  host: https://splunk.acme.com:8089
  index: main
  sourcetype: security_alerts
  # Credentials are loaded from the environment, not stored here.
  # Set: ALERTTRIAGE_ACME_CORP_API_KEY=<splunk-token>

# Cost limits — STRONGLY recommended for production.
# The API returns 402 when the limit would be exceeded.
# null disables the limit for that period.
cost_limits:
  daily_usd: 10.0          # hard stop at $10/day
  monthly_usd: 200.0       # hard stop at $200/month

# Anonymise PII before sending to AI — always true in production.
anonymize: true

# Client tier — used for dashboard grouping and report labelling.
tier: standard             # standard | premium | enterprise

# Per-client learning engine overrides.
# Inherits global defaults from default_config.yaml if omitted.
learning:
  fp_rate_threshold: 0.45    # lower = hints generated sooner
  min_sample_size: 3         # lower = hints generated with fewer samples
  max_hints: 8
  cache_ttl_sec: 300

# Per-client feature flag overrides.
# Only set flags you want to change from the global default.
features:
  auto_dismiss:
    enabled: false           # enable when you trust the AI's confidence

# Per-client alert-type threshold overrides.
# Only set rules you want to change from alert_types.yaml.
alert_types:
  port_scan_detected:
    confidence_threshold: 0.55   # more lenient for this noisy rule
    notes: "Acme runs frequent internal network scans"

# Arbitrary metadata — not used by the engine, shown in reports.
metadata:
  soc_team: "security@acme.com"
  contract_id: "C-2026-001"
```

Save the file. The API hot-reloads config within seconds (no restart needed).

**Verify the config is valid:**

```bash
curl -s -H "X-API-Key: YOUR_KEY" \
  http://localhost:8000/config/acme-corp | python -m json.tool
```

If the config has a validation error, you will see an error response. Fix the YAML and re-check.

---

## Step 2: Set SIEM Credentials

SIEM credentials are always environment variables, never stored in YAML files.

The variable name pattern is: `ALERTTRIAGE_<CLIENT_ID_UPPERCASE>_API_KEY`  
Hyphens in the client ID become underscores.

For client `acme-corp`:

```bash
export ALERTTRIAGE_ACME_CORP_API_KEY=splunk-token-abc123

# Or in .env:
echo 'ALERTTRIAGE_ACME_CORP_API_KEY=splunk-token-abc123' >> .env
```

To rotate credentials, update the environment variable and restart the API (the variable is read at process start).

---

## Step 3: Set Cost Limits

Cost limits are enforced before every AI call. If `daily_usd` or `monthly_usd` is exceeded, the API returns a `402 Payment Required` error — the alert is not analysed.

Set limits in the client YAML:

```yaml
cost_limits:
  daily_usd: 10.0      # roughly 1,000 Sonnet analyses at ~$0.01 each
  monthly_usd: 200.0
```

**Check current spend at any time:**

```bash
curl -s -H "X-API-Key: YOUR_KEY" \
  http://localhost:8000/dashboard/health | python -m json.tool
```

Or look at `data/acme-corp/cost.json`:

```json
{"daily_usd": 3.42, "monthly_usd": 41.07, "day": "2026-05-25", "month": "2026-05"}
```

Daily and monthly totals reset automatically on the first request after midnight / month change — no cron job needed.

---

## Step 4: Configure Alert-Type Thresholds

Global thresholds live in `config/alert_types.yaml`. Override individual rules in the client YAML under `alert_types:`.

**Key thresholds:**

| Parameter | Default | Effect |
|-----------|---------|--------|
| `confidence_threshold` | 0.70 | Minimum AI confidence to accept the verdict |
| `auto_dismiss` | false | Auto-dismiss when confidence exceeds `auto_dismiss_confidence` |
| `auto_dismiss_confidence` | 0.97 | Confidence threshold for auto-dismiss |
| `escalate_on_low_confidence` | false | Escalate when confidence is below threshold |

**Example: Make a noisy rule more lenient for this client:**

```yaml
# In config/client_configs/acme-corp.yaml
alert_types:
  failed_login:
    confidence_threshold: 0.55   # default is 0.65; lower = fewer "low confidence" escalations
    notes: "Acme has high auth activity from CI/CD pipelines"
```

**Update via API without editing files:**

```bash
curl -s -X PUT \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  "http://localhost:8000/config/alert-types/failed_login?client_id=acme-corp" \
  -d '{"confidence_threshold": 0.55, "notes": "High CI/CD auth activity"}'
```

---

## Step 5: Test with dry_run

`dry_run=true` runs the full pipeline (config loading, PII anonymisation, cost check) but skips the AI call and returns a placeholder result. Use this to verify connectivity before spending API credits.

```bash
curl -s -X POST http://localhost:8000/analyze \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": "acme-corp",
    "rule_name": "brute_force_login",
    "severity": "high",
    "title": "Test alert",
    "description": "This is a dry-run test",
    "source": "manual",
    "dry_run": true
  }' | python -m json.tool
```

**Expected response:**

```json
{
  "verdict": "unknown",
  "confidence": 0.0,
  "summary": "[dry-run] No AI call made.",
  "model_id": "dry-run",
  ...
}
```

If you get a `503`, the API is still starting up. If you get a `402`, the client has hit a cost limit. If you get a `404` on the config endpoint, check the YAML filename matches the `client_id`.

---

## Step 6: Run Your First Real Triage Batch

Once dry-run passes, submit a real alert:

```bash
curl -s -X POST http://localhost:8000/analyze \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": "acme-corp",
    "rule_name": "brute_force_login",
    "severity": "high",
    "title": "Multiple failed SSH logins from 203.0.113.5",
    "description": "50 failed SSH login attempts from 203.0.113.5 to web-01 in 5 minutes. IP not in known ranges.",
    "source": "splunk",
    "context": {
      "host_info": {"hostname": "web-01", "role": "web server"},
      "network_info": {"src_ip": "203.0.113.5", "dst_port": 22}
    },
    "tags": ["ssh", "brute-force", "T1110"]
  }' | python -m json.tool
```

Save the `alert_id` and `id` (analysis ID) from the response — you will need them for feedback.

**For batch processing via the CLI runner:**

```bash
python scripts/run_client.py --client-id acme-corp --limit 100
```

---

## Step 7: Record Analyst Feedback

After an analyst reviews a triaged alert, record their verdict. This is what drives the learning engine.

```bash
curl -s -X POST http://localhost:8000/feedback \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "alert_id": "ALERT_ID_FROM_ANALYZE",
    "analysis_id": "ANALYSIS_ID_FROM_ANALYZE",
    "client_id": "acme-corp",
    "analyst_id": "analyst@acme.com",
    "analyst_verdict": "true_positive",
    "analyst_notes": "Confirmed attack — source IP blocked at firewall",
    "ai_verdict_was_correct": true,
    "rule_name": "brute_force_login"
  }'
```

After 5+ feedback records on a rule, the learning engine starts generating hints. View current hints:

```bash
curl -s -H "X-API-Key: YOUR_KEY" \
  "http://localhost:8000/hints?client_id=acme-corp" | python -m json.tool
```

**Bulk feedback import** (from a SOAR export) is most efficient via the Python API:

```python
from pathlib import Path
from alerttriage.src.feedback.feedback_system import FeedbackSystem
from alerttriage.src.core.alert_models import FeedbackRecord

fs = FeedbackSystem(Path("data"), "acme-corp")
records = [...]  # list of FeedbackRecord objects built from your SOAR export
fs.record_many(records)  # single transaction, much faster than record() in a loop
```

---

## Step 8: Set Up Your First Webhook

Webhooks let AlertTriage push events to your SOAR, ticketing system, or Slack channel without polling.

**Register a webhook:**

```bash
curl -s -X POST http://localhost:8000/webhooks \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": "acme-corp",
    "url": "https://hooks.example.com/alerttriage",
    "events": ["alert_analyzed", "threshold_exceeded"],
    "secret": "my-hmac-signing-secret",
    "description": "Main SOC notification endpoint"
  }'
```

Save the `id` from the response (webhook ID).

**Test delivery:**

```bash
curl -s -X POST \
  -H "X-API-Key: YOUR_KEY" \
  "http://localhost:8000/webhooks/WEBHOOK_ID/test" | python -m json.tool
```

If `"success": false`, check the `error` field. Common causes: unreachable URL, TLS certificate error, or the endpoint returned a 4xx.

**Verify the signature in your receiver:**

```python
import hashlib
import hmac
from flask import request

@app.route("/alerttriage", methods=["POST"])
def receive():
    payload = request.get_data()
    sig = request.headers.get("X-AlertTriage-Signature", "")
    secret = "my-hmac-signing-secret"
    expected = "sha256=" + hmac.new(
        secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return "Forbidden", 403
    event = request.json
    # process event...
    return "OK", 200
```

---

## Step 9: View Analytics

**Check overall accuracy:**

```bash
curl -s -H "X-API-Key: YOUR_KEY" \
  http://localhost:8000/stats/acme-corp | python -m json.tool
```

**Get ROI calculation (last 30 days, $75/hr analyst rate, 15 min/alert):**

```bash
curl -s -H "X-API-Key: YOUR_KEY" \
  "http://localhost:8000/analytics/roi/acme-corp?days=30&hourly_rate=75&minutes_per_alert=15" \
  | python -m json.tool
```

**Download an HTML report:**

```bash
curl -s -H "X-API-Key: YOUR_KEY" \
  "http://localhost:8000/analytics/report/acme-corp/download?format=html&days=30" \
  -o acme-corp-report.html && open acme-corp-report.html
```

**Set up weekly email reports:**

```bash
curl -s -X POST \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  "http://localhost:8000/analytics/schedule/acme-corp" \
  -d '{
    "frequency": "weekly",
    "day_of_week": 1,
    "hour": 9,
    "email": "soc@acme.com",
    "format": "html",
    "analyst_hourly_rate": 75.0,
    "enabled": true
  }'
```

This requires `ALERTTRIAGE_SMTP_HOST` and related SMTP variables to be set.

---

## Ongoing Operations

### Rotating API Keys

Update `ALERTTRIAGE_API_KEYS` (comma-separated) and restart the API. Old keys are invalid immediately on restart.

### Rotating SIEM Credentials

Update `ALERTTRIAGE_ACME_CORP_API_KEY` and restart the API.

### Updating Config Without Restart

Edit the YAML file and either wait for hot-reload (up to 30 seconds) or trigger it immediately:

```bash
curl -s -X POST -H "X-API-Key: YOUR_KEY" http://localhost:8000/config/reload
```

### Rolling Back a Config Change

```bash
# List available snapshots
curl -s -H "X-API-Key: YOUR_KEY" \
  http://localhost:8000/config/acme-corp/versions | python -m json.tool

# Roll back to a specific version
curl -s -X POST -H "X-API-Key: YOUR_KEY" \
  "http://localhost:8000/config/acme-corp/rollback/VERSION_ID"
```

### Checking Accuracy Trends

When accuracy drops below 70% (default threshold), an alert fires via Slack/email. To investigate the cause, check the breakdown for high-FP rules:

```bash
curl -s -H "X-API-Key: YOUR_KEY" \
  "http://localhost:8000/analytics/fp-analysis/acme-corp?days=14" | python -m json.tool
```

Rules with high FP rates will start earning prompt hints automatically once they cross the `fp_rate_threshold` (default 50%) with at least `min_sample_size` (default 5) samples.
