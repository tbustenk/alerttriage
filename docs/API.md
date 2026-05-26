# AlertTriage v2 — API Reference

This document covers the AlertTriage REST API. Interactive documentation is also available at `http://localhost:8000/docs` (Swagger UI) and `http://localhost:8000/redoc` when the server is running.

## Table of Contents

- [Authentication](#authentication)
- [Rate Limiting](#rate-limiting)
- [Analysis](#analysis)
- [Feedback](#feedback)
- [Analytics](#analytics)
- [Webhooks](#webhooks)
- [Configuration](#configuration)
- [Operations](#operations)
- [Error Codes](#error-codes)
- [Webhook Event Payload Schemas](#webhook-event-payload-schemas)

---

## Authentication

All endpoints except `GET /health` and `GET /metrics` require an `X-API-Key` header.

```bash
curl -H "X-API-Key: YOUR_KEY" http://localhost:8000/stats/acme-corp
```

Keys are configured via the `ALERTTRIAGE_API_KEYS` environment variable (comma-separated). When that variable is not set, the API runs in open-access mode and returns a warning in logs.

**Responses on authentication failure:**

| Condition | Status | Body |
|-----------|--------|------|
| Header missing | 401 | `{"detail": "Missing X-API-Key header"}` |
| Key invalid | 403 | `{"detail": "Invalid API key"}` |

---

## Rate Limiting

Requests are limited per source IP using a sliding 60-second window.

| Header in 429 response | Value |
|------------------------|-------|
| `Retry-After` | `60` |

Default: 60 requests/minute. Configure via `ALERTTRIAGE_API_RATE_LIMIT`.

**429 response body:**
```json
{"detail": "Rate limit exceeded: 60 requests/minute allowed"}
```

---

## Analysis

### POST /analyze

Submit an alert for AI triage. The pipeline runs: budget check → PII anonymisation → prompt enhancement with learned hints → AI model call.

**Request body:**

```json
{
  "client_id": "acme-corp",
  "rule_name": "brute_force_login",
  "severity": "high",
  "title": "Multiple failed SSH logins",
  "description": "50 failed SSH login attempts from 1.2.3.4 in 5 minutes",
  "source": "splunk",
  "raw_payload": {},
  "tags": ["ssh", "brute-force"],
  "context": {
    "host_info": {"hostname": "web-01", "os": "Ubuntu 22.04"},
    "user_info": {"username": "root"},
    "network_info": {"src_ip": "1.2.3.4"},
    "threat_intel": {},
    "related_alerts": [],
    "raw_logs": []
  },
  "source_alert_id": "splunk-evt-9876",
  "dry_run": false
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `client_id` | string | Yes | Client identifier (must match a config file) |
| `rule_name` | string | Yes | Detection rule that fired |
| `severity` | string | Yes | `critical`, `high`, `medium`, `low`, or `info` |
| `title` | string | Yes | Human-readable alert title |
| `description` | string | Yes | Alert body/description |
| `source` | string | No | `splunk`, `elk`, `webhook`, `manual`, `siem_generic` (default: `manual`) |
| `raw_payload` | object | No | Full SIEM event payload |
| `tags` | array | No | MITRE ATT&CK tags or custom labels |
| `context` | object | No | Enrichment: host, user, network, threat intel, related alerts, raw logs |
| `source_alert_id` | string | No | Original SIEM alert ID for correlation |
| `dry_run` | boolean | No | Skip AI call; return a placeholder result (default: `false`) |

**curl example:**

```bash
curl -s -X POST http://localhost:8000/analyze \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": "acme-corp",
    "rule_name": "brute_force_login",
    "severity": "high",
    "title": "Multiple failed SSH logins",
    "description": "50 failed SSH login attempts from 1.2.3.4 in 5 minutes",
    "source": "splunk"
  }' | python -m json.tool
```

**Response (200):**

```json
{
  "id": "analysis-uuid",
  "alert_id": "alert-uuid",
  "client_id": "acme-corp",
  "model_id": "claude-sonnet-20250219",
  "verdict": "true_positive",
  "confidence": 0.92,
  "summary": "High-confidence brute force attack: 50 failed SSH logins from a known malicious IP.",
  "reasoning": "The source IP 1.2.3.4 attempted 50 logins in 5 minutes targeting root. This pattern is consistent with automated credential stuffing...",
  "risk_factors": [
    {"name": "High frequency", "description": "50 attempts in 5 minutes", "weight": 0.9},
    {"name": "Root targeting", "description": "Targeting privileged account", "weight": 0.85}
  ],
  "recommended_actions": [
    {"priority": 1, "action": "Block source IP", "rationale": "Confirmed malicious source"},
    {"priority": 2, "action": "Review auth logs", "rationale": "Check for successful logins"}
  ],
  "prompt_tokens": 1250,
  "completion_tokens": 380,
  "cost_usd": 0.0023,
  "latency_ms": 1840,
  "timestamp": "2026-05-25T10:30:00Z"
}
```

**Verdict values:** `true_positive`, `false_positive`, `needs_escalation`, `needs_investigation`, `benign`, `unknown`

---

### GET /results/{alert_id}

Retrieve a cached analysis result by the alert's UUID.

Results are held in an in-memory LRU cache (up to 10,000 entries). Results are lost on server restart.

```bash
curl -H "X-API-Key: YOUR_KEY" \
  http://localhost:8000/results/alert-uuid-here
```

**Response:** Same schema as `POST /analyze`.  
**404** if the result is not in cache.

---

### POST /context

Store persistent environment context for a client. Context is automatically injected into future `POST /analyze` calls for the same client.

```bash
curl -s -X POST http://localhost:8000/context \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": "acme-corp",
    "context_type": "network_ranges",
    "data": {"internal_ranges": ["10.0.0.0/8", "192.168.0.0/16"]},
    "description": "Corporate internal network ranges"
  }'
```

| `context_type` | Injected as | Notes |
|----------------|------------|-------|
| `network_ranges` | `context.network_info` | Internal IP ranges for false-positive reduction |
| `known_services` | `context.host_info` | Known hostnames and services |
| `threat_intel` | `context.threat_intel` | Custom threat intelligence data |
| other | Stored but not auto-injected | Available for custom extensions |

Submitting the same `context_type` replaces the previous entry.

**Response (201):**

```json
{"status": "stored", "client_id": "acme-corp", "context_type": "network_ranges"}
```

---

## Feedback

### POST /feedback

Record an analyst's verdict for a previously triaged alert. After recording, the prompt hint cache is invalidated so the learning engine rebuilds on the next analysis.

```bash
curl -s -X POST http://localhost:8000/feedback \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "alert_id": "alert-uuid",
    "analysis_id": "analysis-uuid",
    "client_id": "acme-corp",
    "analyst_id": "analyst@acme.com",
    "analyst_verdict": "false_positive",
    "analyst_notes": "Known vulnerability scanner, added to allowlist",
    "ai_verdict_was_correct": false,
    "rule_name": "port_scan_detected"
  }'
```

| `analyst_verdict` value | Meaning |
|------------------------|---------|
| `true_positive` | Alert is a real threat |
| `false_positive` | Alert is a false alarm |
| `escalated` | Alert requires senior analyst review |
| `closed` | Alert has been investigated and closed |

**Response (201):**

```json
{"status": "recorded", "id": "feedback-uuid"}
```

---

## Analytics

All analytics endpoints accept `?days=` (1–365) and `?hourly_rate=` / `?minutes_per_alert=` for ROI calculations.

### GET /stats/{client_id}

Overall accuracy and per-rule false-positive statistics.

```bash
curl -H "X-API-Key: YOUR_KEY" http://localhost:8000/stats/acme-corp
```

**Response:**

```json
{
  "client_id": "acme-corp",
  "overall_accuracy": 0.87,
  "total_analyses": 342,
  "rule_stats": [
    {
      "rule_name": "port_scan_detected",
      "total": 45,
      "correct": 18,
      "false_positives": 27,
      "fp_rate": 0.60,
      "accuracy": 0.40,
      "latest_ts": "2026-05-25T09:00:00"
    }
  ]
}
```

Rules are sorted by `fp_rate` descending — highest-noise rules appear first.

---

### GET /hints

Learned prompt hints currently active for a client.

```bash
curl -H "X-API-Key: YOUR_KEY" \
  "http://localhost:8000/hints?client_id=acme-corp"
```

**Response:**

```json
{
  "client_id": "acme-corp",
  "hints": [
    "port_scan_detected has a 60% false-positive rate (45 samples). Lean toward benign for internal scanner IPs.",
    "ssl_certificate_error fires mostly on expired internal certs. Check issuer before escalating."
  ],
  "generated_at": "2026-05-25T10:30:00Z"
}
```

An empty `hints` list means no rules have crossed the false-positive threshold yet.

---

### GET /analytics/roi/{client_id}

Return-on-investment calculation over the specified period.

```bash
curl -H "X-API-Key: YOUR_KEY" \
  "http://localhost:8000/analytics/roi/acme-corp?days=30&hourly_rate=75&minutes_per_alert=15"
```

**Response:**

```json
{
  "client_id": "acme-corp",
  "generated_at": "2026-05-25T10:30:00Z",
  "period_days": 30,
  "alerts_analyzed": 342,
  "analyst_minutes_per_alert": 15.0,
  "analyst_hourly_rate": 75.0,
  "hours_saved": 85.5,
  "labor_value_usd": 6412.50,
  "ai_cost_usd": 47.83,
  "net_roi_usd": 6364.67,
  "roi_percentage": 1330.4,
  "payback_ratio": 134.1,
  "monthly_projection_usd": 6364.67
}
```

---

### GET /analytics/breakdown/{client_id}

Per-rule statistics: FP rate, accuracy, trend, volume.

```bash
curl -H "X-API-Key: YOUR_KEY" \
  "http://localhost:8000/analytics/breakdown/acme-corp?days=30"
```

---

### GET /analytics/fp-analysis/{client_id}

False-positive pattern analysis with recommendations.

```bash
curl -H "X-API-Key: YOUR_KEY" \
  "http://localhost:8000/analytics/fp-analysis/acme-corp?days=30"
```

---

### GET /analytics/summary/{client_id}

Weekly performance summary: throughput, accuracy, hours saved, labor value, AI cost, net ROI.

```bash
curl -H "X-API-Key: YOUR_KEY" \
  "http://localhost:8000/analytics/summary/acme-corp?weeks=4&hourly_rate=75"
```

---

### GET /analytics/trends/{client_id}

Weekly accuracy, FP rate, and throughput trend data.

```bash
curl -H "X-API-Key: YOUR_KEY" \
  "http://localhost:8000/analytics/trends/acme-corp?days=90"
```

---

### GET /analytics/report/{client_id}

Full analytics report as JSON.

```bash
curl -H "X-API-Key: YOUR_KEY" \
  "http://localhost:8000/analytics/report/acme-corp?days=30"
```

---

### GET /analytics/report/{client_id}/download

Download the full report in a specific format.

```bash
# HTML (default)
curl -H "X-API-Key: YOUR_KEY" \
  "http://localhost:8000/analytics/report/acme-corp/download?format=html" \
  -o report.html

# PDF (requires fpdf2 installed)
curl -H "X-API-Key: YOUR_KEY" \
  "http://localhost:8000/analytics/report/acme-corp/download?format=pdf" \
  -o report.pdf

# CSV
curl -H "X-API-Key: YOUR_KEY" \
  "http://localhost:8000/analytics/report/acme-corp/download?format=csv" \
  -o report.csv
```

**Query parameters:** `format` (`html`, `pdf`, `csv`, `json`), `days`, `hourly_rate`, `minutes_per_alert`.

---

### GET /analytics/schedule/{client_id}

Get the scheduled report configuration for a client.

### POST /analytics/schedule/{client_id}

Create or update a scheduled report.

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

### DELETE /analytics/schedule/{client_id}

Remove the scheduled report for a client.

---

## Webhooks

### GET /webhooks/event-types

List all valid event type strings. No authentication required.

```bash
curl http://localhost:8000/webhooks/event-types
# ["alert_analyzed", "feedback_recorded", "accuracy_changed",
#  "threshold_exceeded", "report_generated", "cost_limit_approaching"]
```

---

### POST /webhooks

Register a webhook endpoint.

```bash
curl -s -X POST http://localhost:8000/webhooks \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": "acme-corp",
    "url": "https://hooks.example.com/alerttriage",
    "events": ["alert_analyzed", "threshold_exceeded"],
    "secret": "my-hmac-secret",
    "description": "SOC notification endpoint"
  }'
```

**Response (201):**

```json
{
  "id": "webhook-uuid",
  "client_id": "acme-corp",
  "url": "https://hooks.example.com/alerttriage",
  "events": ["alert_analyzed", "threshold_exceeded"],
  "description": "SOC notification endpoint",
  "active": true,
  "has_secret": true,
  "consecutive_failures": 0,
  "last_success_at": null,
  "last_failure_at": null,
  "created_at": "2026-05-25T10:00:00Z"
}
```

---

### GET /webhooks

List all registered webhooks. Filter by client with `?client_id=acme-corp`.

### GET /webhooks/{webhook_id}

Get a specific webhook registration.

### PATCH /webhooks/{webhook_id}

Update a webhook (URL, events, secret, active status).

```bash
curl -s -X PATCH \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  "http://localhost:8000/webhooks/webhook-uuid" \
  -d '{"active": false}'
```

### DELETE /webhooks/{webhook_id}

Delete a webhook registration (204 No Content).

### POST /webhooks/{webhook_id}/test

Deliver a synthetic `alert_analyzed` test event and return the delivery result.

```bash
curl -s -X POST \
  -H "X-API-Key: YOUR_KEY" \
  "http://localhost:8000/webhooks/webhook-uuid/test"
```

**Response:**

```json
{
  "webhook_id": "webhook-uuid",
  "event_id": "event-uuid",
  "success": true,
  "status_code": 200,
  "attempts": 1,
  "error": null,
  "delivered_at": "2026-05-25T10:30:00Z"
}
```

---

## Configuration

### GET /config/{client_id}

Get the current resolved configuration for a client.

```bash
curl -H "X-API-Key: YOUR_KEY" http://localhost:8000/config/acme-corp
```

### PUT /config/{client_id}

Update a client's configuration. A snapshot is automatically saved before applying the change.

```bash
curl -s -X PUT \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  "http://localhost:8000/config/acme-corp" \
  -d '{
    "config": {
      "client_id": "acme-corp",
      "model": "claude-sonnet",
      "cost_limits": {"daily_usd": 10.0, "monthly_usd": 200.0}
    },
    "comment": "Increase monthly limit"
  }'
```

### GET /config/{client_id}/versions

List config version history for a client (newest first).

### GET /config/{client_id}/versions/{version_id}

Get a specific config snapshot.

### POST /config/{client_id}/rollback/{version_id}

Roll back to a previous config version.

### POST /config/{client_id}/snapshot

Manually create a config snapshot.

```bash
curl -s -X POST \
  -H "X-API-Key: YOUR_KEY" \
  "http://localhost:8000/config/acme-corp/snapshot?comment=before+migration"
```

### GET /config/features/global

List all feature flags with global defaults.

### GET /config/features/{client_id}

List effective feature flags for a client (with per-client overrides applied).

### PUT /config/features/{client_id}

Update feature flag overrides for a client.

```bash
curl -s -X PUT \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  "http://localhost:8000/config/features/acme-corp" \
  -d '{"auto_dismiss": {"enabled": true, "confidence_threshold": 0.97}}'
```

### GET /config/alert-types/all

List all configured alert-type rule names.

### GET /config/alert-types/{rule_name}

Get threshold settings for an alert type. Pass `?client_id=acme-corp` for client-specific effective settings.

### PUT /config/alert-types/{rule_name}

Update alert-type settings. Pass `?client_id=acme-corp` to scope to one client.

### POST /config/reload

Force a hot-reload of all config files without restarting the API.

```bash
curl -s -X POST -H "X-API-Key: YOUR_KEY" http://localhost:8000/config/reload
```

### GET /config/clients

List all configured client IDs.

---

## Operations

### GET /health

System health check. **No authentication required.** Safe for load balancer probes.

```bash
curl http://localhost:8000/health
```

**Response:**

```json
{
  "status": "healthy",
  "version": "2.0.0",
  "uptime_seconds": 3600.0,
  "components": [
    {"name": "database", "healthy": true, "latency_ms": 2.1, "details": ""},
    {"name": "feedback_system", "healthy": true, "latency_ms": null, "details": ""},
    {"name": "api", "healthy": true, "latency_ms": 0.5, "details": ""}
  ],
  "timestamp": "2026-05-25T10:30:00Z"
}
```

**`status` values:** `healthy`, `degraded`, `unhealthy`

---

### GET /dashboard/health

Full health and performance dashboard. Requires authentication. Suitable for Grafana JSON datasource.

```bash
curl -H "X-API-Key: YOUR_KEY" http://localhost:8000/dashboard/health
```

Returns: uptime, latency percentiles (p50/p95/p99), per-client accuracy, per-client cost, component health, threshold breaches.

---

### GET /metrics

Prometheus text-format metrics. **No authentication required.**

```bash
curl http://localhost:8000/metrics
```

Key metrics exported:

| Metric | Type | Description |
|--------|------|-------------|
| `alerttriage_requests_total` | Counter | Total API requests |
| `alerttriage_latency_p50_ms` | Gauge | Median latency |
| `alerttriage_latency_p95_ms` | Gauge | 95th-percentile latency |
| `alerttriage_latency_p99_ms` | Gauge | 99th-percentile latency |
| `alerttriage_accuracy{client_id}` | Gauge | Per-client AI accuracy |
| `alerttriage_cost_daily_usd{client_id}` | Gauge | Per-client daily spend |
| `alerttriage_cost_monthly_usd{client_id}` | Gauge | Per-client monthly spend |

---

## Error Codes

| HTTP Status | Meaning | Common cause |
|-------------|---------|--------------|
| 200 | OK | Successful request |
| 201 | Created | Feedback or webhook registered |
| 204 | No Content | Webhook deleted |
| 400 | Bad Request | Malformed JSON |
| 401 | Unauthorized | Missing `X-API-Key` header |
| 403 | Forbidden | Invalid API key |
| 404 | Not Found | Alert result not in cache; webhook ID not found |
| 422 | Unprocessable Entity | Invalid field values (bad severity, blank client_id, etc.) |
| 429 | Too Many Requests | Rate limit exceeded; retry after 60 seconds |
| 500 | Internal Server Error | AI call failed after all retries |
| 503 | Service Unavailable | Component not initialised (API still starting up) |

---

## Webhook Event Payload Schemas

All events share a common envelope:

```json
{
  "id": "evt-uuid",
  "event_type": "alert_analyzed",
  "client_id": "acme-corp",
  "timestamp": "2026-05-25T10:30:00Z",
  "data": { ... }
}
```

### `alert_analyzed`

Fired after every successful AI triage.

```json
{
  "alert_id": "alert-uuid",
  "analysis_id": "analysis-uuid",
  "verdict": "true_positive",
  "confidence": 0.92,
  "model_id": "claude-sonnet-20250219",
  "cost_usd": 0.0023,
  "latency_ms": 1840,
  "summary": "High-confidence brute force attack..."
}
```

### `feedback_recorded`

Fired when an analyst records a verdict via `POST /feedback`.

```json
{
  "alert_id": "alert-uuid",
  "analysis_id": "analysis-uuid",
  "analyst_id": "analyst@acme.com",
  "analyst_verdict": "false_positive",
  "ai_was_correct": false
}
```

### `threshold_exceeded`

Fired when a health threshold is breached (accuracy drops or latency spikes).

```json
{
  "metric": "accuracy",
  "threshold": 0.70,
  "current": 0.62,
  "message": "Accuracy for acme-corp dropped below 70% threshold"
}
```

### `accuracy_changed`

Fired when per-client accuracy crosses a notable boundary.

```json
{
  "client_id": "acme-corp",
  "previous_accuracy": 0.75,
  "current_accuracy": 0.62,
  "total_samples": 342
}
```

### `report_generated`

Fired when a scheduled report is successfully generated and delivered.

```json
{
  "client_id": "acme-corp",
  "format": "html",
  "period_days": 30,
  "email": "soc@acme.com",
  "report_path": "reports/acme-corp/report_20260525.html"
}
```

### `cost_limit_approaching`

Fired when a client's spend reaches 90% of a configured limit.

```json
{
  "client_id": "acme-corp",
  "limit_type": "daily",
  "limit_usd": 10.0,
  "current_usd": 9.1,
  "percentage": 91.0
}
```

### Verifying Webhook Signatures

When a `secret` is set on a webhook registration, every delivery includes:

```
X-AlertTriage-Signature: sha256=<hex-digest>
```

Verify in Python:

```python
import hashlib
import hmac

def verify_webhook(payload: bytes, secret: str, signature_header: str) -> bool:
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature_header, expected)
```

Always verify before processing. Use `hmac.compare_digest` to prevent timing attacks.
