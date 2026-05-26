# AlertTriage v2 — Troubleshooting Guide

## Table of Contents

- [Common Startup Errors](#common-startup-errors)
- [Database Issues](#database-issues)
- [API Errors](#api-errors)
- [Low Accuracy Debugging](#low-accuracy-debugging)
- [Performance Issues](#performance-issues)
- [Docker Issues](#docker-issues)
- [Log Interpretation Guide](#log-interpretation-guide)

---

## Common Startup Errors

### `ModuleNotFoundError: No module named 'alerttriage'`

The package is not installed in the current environment, or the import path is wrong.

**Fix:**

```bash
# Install in editable mode from the repo root
pip install -e .

# Verify
python -c "import alerttriage; print('OK')"
```

The repository directory must be named `alerttriage` (not `alerttriage-main` or similar). Imports are rooted at the parent of the repo.

---

### `ConfigError: No such file: config/default_config.yaml`

The API cannot find the config directory.

**Fix:** Ensure the working directory is the repo root when running the API, or set `ALERTTRIAGE_CONFIG_DIR`:

```bash
export ALERTTRIAGE_CONFIG_DIR=/absolute/path/to/alerttriage/config
python scripts/run_api.py
```

In Docker, the config directory is bind-mounted at `/app/alerttriage/config`. If `ALERTTRIAGE_CONFIG_DIR` is not set, the default is resolved relative to `src/api/app.py`, which is `/app/alerttriage/config` inside the container.

---

### `ValidationError: ... client_id must not be blank`

A client config YAML has an empty or missing `client_id` field.

**Fix:** Check all files in `config/client_configs/`. The `client_id` value must be a non-empty lowercase string matching the filename:

```yaml
# config/client_configs/acme-corp.yaml
client_id: acme-corp   # must match filename (without .yaml)
```

---

### `WARNING: api_open_access — ALERTTRIAGE_API_KEYS not set`

This is a warning, not an error. The API starts successfully but accepts all requests without authentication.

**Fix for production:** Set `ALERTTRIAGE_API_KEYS` to one or more comma-separated keys:

```bash
export ALERTTRIAGE_API_KEYS=$(python -c "import secrets; print(secrets.token_hex(32))")
```

---

### `anthropic.AuthenticationError` or `openai.AuthenticationError`

The AI provider API key is missing or invalid.

**Fix:**

```bash
# Test Anthropic key
python -c "import anthropic; c = anthropic.Anthropic(); print('OK')"

# Test OpenAI key
python -c "import openai; c = openai.OpenAI(); print('OK')"
```

Ensure `ANTHROPIC_API_KEY` and/or `OPENAI_API_KEY` are set in the environment or `.env` file.

---

### API starts but `GET /health` returns `503`

The health monitor has not finished initialising. This typically resolves within 15 seconds.

If it persists, check the startup logs for errors. A common cause is a corrupt feedback database being detected and recovered during startup. Check for `feedback_db_corrupt` log events.

---

## Database Issues

### `sqlite3.DatabaseError: database disk image is malformed`

The SQLite feedback database is corrupt (typically caused by an abrupt process kill or disk full event).

**How AlertTriage handles this:** On startup, a corrupt database is automatically moved to `data/<client_id>/feedback.db.corrupt-<timestamp>` and a fresh empty database is created. The service continues running.

**Investigation:**

```bash
# Locate the corrupt file
ls data/acme-corp/

# Try to read it
sqlite3 data/acme-corp/feedback.db.corrupt-1748121600 ".tables"

# If readable, dump and reimport
sqlite3 data/acme-corp/feedback.db.corrupt-1748121600 \
  "SELECT * FROM feedback" > recovered_feedback.csv
```

**Manual recovery:**

```bash
# Delete the corrupt file if you don't need it
rm data/acme-corp/feedback.db.corrupt-*

# The fresh database is already in place — no action needed
```

---

### Feedback database is growing very large

Each feedback record is small (~500 bytes), but millions of records accumulate over time.

**Check size:**

```bash
ls -lh data/acme-corp/feedback.db
sqlite3 data/acme-corp/feedback.db "SELECT COUNT(*) FROM feedback;"
```

**Vacuum and analyse:**

```bash
sqlite3 data/acme-corp/feedback.db "VACUUM; ANALYZE;"
```

**Archive old records** (example: keep only the last 90 days):

```bash
sqlite3 data/acme-corp/feedback.db \
  "DELETE FROM feedback WHERE timestamp < datetime('now', '-90 days');"
sqlite3 data/acme-corp/feedback.db "VACUUM;"
```

---

### `FeedbackStoreError: feedback DB is corrupt and could not be archived`

The corrupt database could not be moved aside, likely due to a permissions issue.

**Fix:**

```bash
# Check permissions
ls -la data/acme-corp/

# Fix ownership (replace 'alerttriage' with your service user)
chown -R alerttriage:alerttriage data/
chmod 755 data/acme-corp/
```

---

### `cost.json` shows incorrect spend

The cost state file may be stale if the service was not shut down gracefully.

**Inspect:**

```bash
cat data/acme-corp/cost.json
```

**Manual reset** (safe — resets only the in-memory state file, not actual AI billing):

```bash
# Reset daily counter to zero
python -c "
import json
from pathlib import Path
p = Path('data/acme-corp/cost.json')
from datetime import date
data = json.loads(p.read_text())
data['daily_usd'] = 0.0
data['day'] = str(date.today())
p.write_text(json.dumps(data))
print('Reset daily spend to 0.0')
"
```

---

## API Errors

### 401 Unauthorized — Missing X-API-Key header

You forgot the authentication header.

```bash
# Wrong:
curl http://localhost:8000/stats/acme-corp

# Correct:
curl -H "X-API-Key: YOUR_KEY" http://localhost:8000/stats/acme-corp
```

---

### 403 Forbidden — Invalid API key

The key you provided is not in `ALERTTRIAGE_API_KEYS`.

**Verify:** The server loads keys at startup. Changing the env var requires a restart. Check that there are no extra spaces in the key value.

---

### 429 Too Many Requests

You have exceeded the rate limit for your source IP.

**Default:** 60 requests per minute.  
**Response header:** `Retry-After: 60`

**Fix:** Wait 60 seconds, or increase the limit:

```bash
export ALERTTRIAGE_API_RATE_LIMIT=200
# Restart the API to pick up the new value
```

For SOAR integrations that send bursts of alerts, use the `analyze_batch()` endpoint (CLI runner) rather than individual `POST /analyze` calls.

---

### 422 Unprocessable Entity

The request body failed validation. The response body contains a detailed error:

```json
{"detail": [{"loc": ["body", "severity"], "msg": "Input should be 'critical', 'high', 'medium', 'low' or 'info'"}]}
```

**Common causes:**

| Field | Valid values |
|-------|-------------|
| `severity` | `critical`, `high`, `medium`, `low`, `info` |
| `source` | `splunk`, `elk`, `webhook`, `manual`, `siem_generic` |
| `client_id` | Non-empty string (lowercase enforced) |
| `analyst_verdict` | `true_positive`, `false_positive`, `escalated`, `closed` |

---

### 500 Internal Server Error — Analysis failed

The AI call failed after all retries and fallbacks.

**Check logs for:**

```
event="backend_failed" model="claude-sonnet" error="..."
event="model_unavailable" client="acme-corp"
```

**Common causes:**

1. **Anthropic API outage** — check `status.anthropic.com`.
2. **Rate limit from Anthropic** — you have hit Anthropic's per-minute token limit. Reduce `concurrency.max_in_flight` or add delays between batch calls.
3. **Context window exceeded** — the alert description or raw_payload is too large. Truncate `raw_payload` before sending.

---

### 503 Service Unavailable — Analyzer not initialised

The API is still starting up or a critical component failed to initialise.

Wait 15-30 seconds and retry. If it persists, check startup logs:

```bash
# Docker
docker logs alerttriage-api --tail 50

# Systemd
journalctl -u alerttriage -n 50
```

---

### 402 Payment Required — Cost limit exceeded

The client has hit its daily or monthly AI spend cap.

**Check current spend:**

```bash
cat data/acme-corp/cost.json
```

**Options:**
1. Wait until midnight for the daily limit to reset automatically.
2. Increase the limit in the client YAML: `cost_limits.daily_usd: 20.0`, then trigger a config reload.
3. Manually set `daily_usd` to `0.0` in `data/acme-corp/cost.json` (emergency reset — counts against the monthly total).

---

## Low Accuracy Debugging

Accuracy is defined as the fraction of AI verdicts that match the analyst's verdict. A value below 70% triggers an alert.

### Step 1: Identify which rules are problematic

```bash
curl -s -H "X-API-Key: YOUR_KEY" \
  http://localhost:8000/stats/acme-corp | python -m json.tool
```

Look at `rule_stats`, sorted by `fp_rate` descending. Rules with `fp_rate > 0.50` are the primary candidates.

---

### Step 2: Check the FP analysis

```bash
curl -s -H "X-API-Key: YOUR_KEY" \
  "http://localhost:8000/analytics/fp-analysis/acme-corp?days=30" | python -m json.tool
```

The `patterns` and `recommendations` fields contain AI-generated analysis of common misclassification patterns.

---

### Step 3: Check if learning hints are being generated

```bash
curl -s -H "X-API-Key: YOUR_KEY" \
  "http://localhost:8000/hints?client_id=acme-corp" | python -m json.tool
```

If `hints` is empty and you have many feedback records, check the learning thresholds:

```yaml
# config/client_configs/acme-corp.yaml
learning:
  fp_rate_threshold: 0.50   # rule must have FP rate >= this
  min_sample_size: 5        # rule must have >= this many records
```

Lower both values to generate hints sooner:

```yaml
learning:
  fp_rate_threshold: 0.35
  min_sample_size: 3
```

---

### Step 4: Add environmental context

If the AI doesn't know about your internal network ranges, it may flag internal scanner activity. Add context:

```bash
curl -s -X POST http://localhost:8000/context \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": "acme-corp",
    "context_type": "network_ranges",
    "data": {
      "internal_ranges": ["10.0.0.0/8", "192.168.0.0/16"],
      "scanner_ips": ["10.0.1.100", "10.0.1.101"]
    }
  }'
```

---

### Step 5: Adjust per-rule confidence thresholds

If a specific rule consistently produces low-confidence verdicts that are wrong:

```bash
curl -s -X PUT \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  "http://localhost:8000/config/alert-types/port_scan_detected?client_id=acme-corp" \
  -d '{"confidence_threshold": 0.55, "escalate_on_low_confidence": true}'
```

---

### Step 6: Try a different model

Some alert types may perform better with a different model:

```yaml
# config/client_configs/acme-corp.yaml
model: claude-opus    # higher accuracy, higher cost
model_fallbacks:
  - claude-sonnet
```

Trigger hot-reload: `curl -X POST -H "X-API-Key: YOUR_KEY" http://localhost:8000/config/reload`

---

## Performance Issues

### High API Latency (p95 > 1 second)

**Identify the bottleneck:**

```bash
curl -s -H "X-API-Key: YOUR_KEY" \
  http://localhost:8000/dashboard/health | python -m json.tool
```

Look at `latency_p50_ms`, `latency_p95_ms`, `latency_p99_ms`.

**Common causes and fixes:**

| Cause | Symptom | Fix |
|-------|---------|-----|
| AI backend slow | All latencies high | Increase `concurrency.max_in_flight`, or switch to a faster model (Haiku) |
| SQLite lock contention | Latency spikes with high concurrency | Enable WAL mode explicitly; reduce concurrency |
| Large prompts | High `latency_ms` in AI call | Truncate `raw_payload` and `raw_logs` before submitting |
| Anthropic rate limit | Periodic 429s from backend | Reduce concurrency; implement client-side rate limiting |

**Enable SQLite WAL mode manually:**

```bash
sqlite3 data/acme-corp/feedback.db "PRAGMA journal_mode=WAL;"
```

---

### `POST /analyze` is slow on the first call for a client

The first call triggers database initialisation and prompt enhancement. Subsequent calls use the cached prompt (TTL 300s). This is expected behaviour.

---

### Memory usage growing continuously

The in-memory result cache (`_result_store`) holds up to 10,000 entries. Each `AnalysisResult` is ~2-5 KB, so maximum cache size is ~50 MB. This is by design and does not leak.

The rate limiter's bucket map grows with unique source IPs. On a public API with many clients, this can accumulate. Restart the API periodically (weekly is typical) to clear the limiter state.

---

## Docker Issues

### Container exits immediately with exit code 1

Check logs:

```bash
docker logs alerttriage-api
```

**Common causes:**

- Missing required env vars (`ANTHROPIC_API_KEY`)
- Config directory not mounted or empty
- Port 8000 already in use on the host

---

### `Permission denied` writing to `/data`

The container runs as the `alerttriage` user (non-root). The volume must be writable by this user.

**Fix:**

```bash
# On the host, ensure the volume is writable
docker run --rm -v alerttriage-data:/data alpine chown -R 1000:1000 /data
```

Or use a named Docker volume instead of a bind mount (named volumes default to root ownership, which the container user can write to):

```yaml
volumes:
  alerttriage-data:
    driver: local
```

---

### Hot-reload not working in Docker

Config changes are not picked up because the config is mounted read-only or the watcher cannot detect changes.

**Trigger a manual reload:**

```bash
curl -s -X POST -H "X-API-Key: YOUR_KEY" http://localhost:8000/config/reload
```

Alternatively, restart the container:

```bash
docker compose restart api
```

---

### `docker compose up` fails with "network not found"

The Docker Compose network was not cleaned up from a previous failed run.

```bash
docker compose down --volumes --remove-orphans
docker compose up
```

---

## Log Interpretation Guide

AlertTriage uses `structlog` for structured key=value logging. In JSON mode (`ALERTTRIAGE_JSON_LOGS=true`), each log line is a parseable JSON object.

### Key Log Events

| Event name | Level | Meaning |
|------------|-------|---------|
| `api_started` | INFO | API started successfully |
| `analyzing_alert` | INFO | Analysis pipeline started for an alert |
| `analysis_complete` | INFO | Analysis finished — includes `verdict`, `confidence`, `cost_usd`, `latency_ms` |
| `feedback_recorded` | INFO | Analyst feedback written to DB |
| `feedback_recorded_batch` | INFO | Bulk feedback import complete |
| `config_changed` | INFO | Hot-reload triggered by file change |
| `api_open_access` | WARNING | `ALERTTRIAGE_API_KEYS` not set — no auth |
| `feedback_db_corrupt` | ERROR | DB was corrupt; moved aside and reinitialised |
| `backend_failed` | ERROR | Single backend call failed (may retry) |
| `cost_limit_hit` | ERROR | Client budget exhausted |
| `siem_unreachable` | ERROR | SIEM health check failed |

### Filtering Logs

```bash
# Stream only errors (Docker)
docker logs -f alerttriage-api 2>&1 | grep '"level":"error"'

# Filter to one client (JSON logs)
docker logs alerttriage-api 2>&1 | \
  python -c "
import sys, json
for line in sys.stdin:
    try:
        obj = json.loads(line)
        if obj.get('client') == 'acme-corp':
            print(json.dumps(obj, indent=2))
    except: pass
"
```

### Tracing a Slow Analysis

Each analysis logs `alert_id` consistently. Grep for it across all events:

```bash
grep "alert-uuid-here" /var/log/alerttriage/api.log
```

You should see `analyzing_alert` and then `analysis_complete` with `latency_ms`. If you only see `analyzing_alert`, the call is still in-flight or the process crashed.
