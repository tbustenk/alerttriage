# AlertTriage — Docker Setup Guide

Get AlertTriage running in a single command. This guide covers the REST API server, the CLI batch runner, the full Compose stack, and production configuration.

---

## Prerequisites

- Docker ≥ 24 and Docker Compose v2 (bundled with Docker Desktop)
- An Anthropic API key (`sk-ant-...`) and/or OpenAI API key

---

## One-command Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/tbustenk/alerttriage && cd alerttriage

# 2. Configure secrets
cp .env.example .env
# Edit .env — set ANTHROPIC_API_KEY at minimum

# 3. Build the image
./scripts/docker_build.sh

# 4a. Start the API server
docker run --env-file .env -p 8000:8000 \
  --entrypoint python alerttriage \
  scripts/run_api.py

# 4b. OR run a one-shot triage batch
docker run --env-file .env alerttriage --client-id acme-corp
```

API is now live at **http://localhost:8000**.  
Swagger UI: **http://localhost:8000/docs**

---

## Building the Image

```bash
# Standard build (tagged alerttriage:latest)
./scripts/docker_build.sh

# With a version tag
./scripts/docker_build.sh --tag v2.1.0

# Force full rebuild (no layer cache)
./scripts/docker_build.sh --no-cache

# Build and push to a registry
ALERTTRIAGE_IMAGE=registry.example.com/alerttriage \
  ./scripts/docker_build.sh --tag v2.1.0 --push

# Cross-platform (for ARM hosts)
./scripts/docker_build.sh --platform linux/amd64,linux/arm64
```

---

## Running the CLI Triage Runner

```bash
# Basic run
docker run --env-file .env alerttriage --client-id acme-corp

# Limit alerts processed
docker run --env-file .env alerttriage --client-id acme-corp --limit 50

# Dry-run (no AI calls, no write-back to SIEM)
docker run --env-file .env alerttriage --client-id acme-corp --dry-run

# Using the helper script
./scripts/docker_run.sh --client-id acme-corp
./scripts/docker_run.sh --client-id acme-corp --dry-run --limit 20
```

---

## Running the REST API

```bash
# Foreground (logs to stdout)
docker run --rm --env-file .env -p 8000:8000 \
  --entrypoint python alerttriage \
  scripts/run_api.py

# Background daemon
./scripts/docker_run.sh --api
./scripts/docker_run.sh --api --port 8080   # custom port

# Stop the background API
docker stop alerttriage-api
```

Once running:

| URL | Description |
|-----|-------------|
| `http://localhost:8000/docs` | Swagger UI |
| `http://localhost:8000/redoc` | ReDoc |
| `http://localhost:8000/health` | Health check |
| `http://localhost:8000/dashboard/health` | Full dashboard (auth required) |
| `http://localhost:8000/metrics` | Prometheus metrics |

---

## Docker Compose (Full Stack)

```bash
# Start the API
docker compose up api

# Background
docker compose up -d api

# With PostgreSQL
docker compose --profile postgres up -d

# Run a one-shot triage job
docker compose run --rm triage --client-id acme-corp

# View logs
docker compose logs -f api

# Stop everything
docker compose down
```

### Compose services

| Service | Profile | Description |
|---------|---------|-------------|
| `api` | *(default)* | REST API on port 8000 |
| `triage` | `triage` | One-shot CLI runner (use `docker compose run`) |
| `postgres` | `postgres` | PostgreSQL database |

---

## Environment Variables

Copy `.env.example` to `.env` and set these:

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes* | Claude API key |
| `OPENAI_API_KEY` | Yes* | GPT API key (*one of the two is required*) |
| `ALERTTRIAGE_API_KEYS` | Recommended | Comma-separated API keys for the REST API |
| `ALERTTRIAGE_ANONYMIZE_SALT` | **Yes** | Random string for PII pseudonymisation |
| `ALERTTRIAGE_LOG_LEVEL` | No | `DEBUG`/`INFO`/`WARNING`/`ERROR` (default: `INFO`) |
| `ALERTTRIAGE_JSON_LOGS` | No | `true` for JSON logs (default: `true` in Docker) |
| `ALERTTRIAGE_DATA_DIR` | No | Data directory (default: `/data` in Docker) |
| `ALERTTRIAGE_REPORTS_DIR` | No | Reports directory (default: `/reports` in Docker) |
| `ALERTTRIAGE_API_RATE_LIMIT` | No | Requests per minute (default: `60`) |
| `ALERTTRIAGE_ACCURACY_THRESHOLD` | No | Alert if accuracy drops below this (default: `0.70`) |
| `ALERTTRIAGE_LATENCY_THRESHOLD_MS` | No | Alert if P95 latency exceeds this (default: `1000`) |
| `ALERTTRIAGE_SLACK_WEBHOOK_URL` | No | Slack webhook for health alerts |
| `ALERTTRIAGE_ALERT_EMAIL_TO` | No | Email address for health alerts |
| `ALERTTRIAGE_CONFIG_DIR` | No | Config directory (default: `config/` in the image) |

### API Authentication

Set `ALERTTRIAGE_API_KEYS` to a comma-separated list of keys:

```bash
ALERTTRIAGE_API_KEYS=key-prod-abc123,key-dev-xyz789
```

Include the key in requests:

```bash
curl -H "X-API-Key: key-prod-abc123" http://localhost:8000/stats/acme-corp
```

If `ALERTTRIAGE_API_KEYS` is empty, the API runs in **open access mode** (fine for local dev, not for production).

---

## Volume Mounts

| Volume | Container path | Description |
|--------|---------------|-------------|
| `alerttriage-data` | `/data` | SQLite feedback DBs, cost tracking files |
| `alerttriage-reports` | `/reports` | Generated HTML/JSON reports |
| `./config` | `/app/alerttriage/config` | YAML config files (read-only) |

To inspect stored data:

```bash
docker run --rm -v alerttriage-data:/data alpine ls /data
```

---

## Adding a Client

1. Create `config/client_configs/<client-id>.yaml` (copy from `config/client_configs/template.yaml`)
2. Rebuild or mount the config directory — the API picks up new files without a restart

```bash
# With docker compose, the config dir is already mounted read-only.
# Just add the file and the API loads it on the next request.
docker compose run --rm triage --client-id new-client-id
```

---

## API Quick Reference

```bash
BASE=http://localhost:8000
KEY=your-api-key-here

# Submit an alert for analysis
curl -s -X POST "$BASE/analyze" \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": "acme-corp",
    "rule_name": "brute_force_login",
    "severity": "high",
    "title": "50 failed SSH logins",
    "description": "50 failed SSH attempts from 1.2.3.4 in 5 minutes",
    "source": "splunk"
  }' | jq .

# Get a cached result
curl -s -H "X-API-Key: $KEY" "$BASE/results/<alert_id>" | jq .

# Record analyst feedback
curl -s -X POST "$BASE/feedback" \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "alert_id": "<alert_id>",
    "analysis_id": "<analysis_id>",
    "client_id": "acme-corp",
    "analyst_id": "alice@acme.com",
    "analyst_verdict": "false_positive",
    "ai_verdict_was_correct": false,
    "rule_name": "brute_force_login"
  }' | jq .

# Get accuracy stats
curl -s -H "X-API-Key: $KEY" "$BASE/stats/acme-corp" | jq .

# Get learned prompt hints
curl -s -H "X-API-Key: $KEY" "$BASE/hints?client_id=acme-corp" | jq .

# Add environment context
curl -s -X POST "$BASE/context" \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": "acme-corp",
    "context_type": "network_ranges",
    "data": {"internal": ["10.0.0.0/8", "192.168.0.0/16"]}
  }' | jq .

# Health check (no auth)
curl -s "$BASE/health" | jq .

# Full dashboard
curl -s -H "X-API-Key: $KEY" "$BASE/dashboard/health" | jq .

# Prometheus metrics
curl -s "$BASE/metrics"
```

---

## Prometheus + Grafana

Add this scrape config to `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: alerttriage
    static_configs:
      - targets: ['alerttriage-api:8000']
    metrics_path: /metrics
```

Key metrics exported:

| Metric | Type | Description |
|--------|------|-------------|
| `alerttriage_uptime_seconds` | gauge | Seconds since process start |
| `alerttriage_alerts_analyzed_total` | counter | Total analyses performed |
| `alerttriage_alerts_analyzed_today` | gauge | Analyses today (UTC) |
| `alerttriage_latency_p50_ms` | gauge | Median analysis latency |
| `alerttriage_latency_p95_ms` | gauge | P95 analysis latency |
| `alerttriage_latency_p99_ms` | gauge | P99 analysis latency |
| `alerttriage_accuracy{client_id}` | gauge | Per-client accuracy (0–1) |
| `alerttriage_cost_today_usd{client_id}` | gauge | Today's AI API spend |
| `alerttriage_cost_month_usd{client_id}` | gauge | This month's AI API spend |
| `alerttriage_component_healthy{component}` | gauge | Component health (0/1) |

---

## Health Alerts

AlertTriage sends notifications when:
- Any component is unhealthy (database down, feedback system error)
- P95 analysis latency exceeds `ALERTTRIAGE_LATENCY_THRESHOLD_MS` (default 1 000 ms)
- Client accuracy drops below `ALERTTRIAGE_ACCURACY_THRESHOLD` (default 70 %)

Configure notifications via environment variables:

```bash
# Slack
ALERTTRIAGE_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...

# Email
ALERTTRIAGE_ALERT_EMAIL_TO=oncall@yourcompany.com
ALERTTRIAGE_ALERT_EMAIL_FROM=alerttriage@yourcompany.com
ALERTTRIAGE_SMTP_HOST=smtp.yourcompany.com
ALERTTRIAGE_SMTP_PORT=587
ALERTTRIAGE_SMTP_USER=alerttriage@yourcompany.com
ALERTTRIAGE_SMTP_PASS=smtp-password
```

---

## Production Checklist

- [ ] Set a strong random `ALERTTRIAGE_ANONYMIZE_SALT`
- [ ] Set `ALERTTRIAGE_API_KEYS` to at least one secret key
- [ ] Set `ALERTTRIAGE_JSON_LOGS=true` and ship logs to your SIEM
- [ ] Mount `./config` as read-only; keep client configs in a secrets manager
- [ ] Back up the `alerttriage-data` volume (contains feedback DBs)
- [ ] Configure Prometheus scraping of `/metrics`
- [ ] Set up Slack or email notifications via `ALERTTRIAGE_SLACK_WEBHOOK_URL`
- [ ] Put a TLS-terminating reverse proxy (nginx, Caddy) in front of the API
- [ ] Set `ALERTTRIAGE_CORS_ORIGINS` to your dashboard domain (not `*`)
