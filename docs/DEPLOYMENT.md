# AlertTriage v2 — Deployment Guide

This guide covers local development setup, Docker deployment, production checklist, and Kubernetes notes. For client onboarding see [CLIENT_SETUP.md](CLIENT_SETUP.md). For the system design see [ARCHITECTURE.md](ARCHITECTURE.md).

## Table of Contents

- [Prerequisites](#prerequisites)
- [Local Development Setup](#local-development-setup)
- [Environment Variables](#environment-variables)
- [Docker Deployment](#docker-deployment)
- [Production Deployment Checklist](#production-deployment-checklist)
- [Kubernetes](#kubernetes)
- [Scaling Recommendations](#scaling-recommendations)
- [Disaster Recovery](#disaster-recovery)

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Python 3.12 | Exact version targeted; 3.11 works but is not tested |
| pip 24+ | For editable installs |
| Docker 24+ | For containerised deployment |
| `ANTHROPIC_API_KEY` | Required for Claude backends |
| `OPENAI_API_KEY` | Required only if using GPT fallback backends |
| Outbound HTTPS | To `api.anthropic.com` and optionally `api.openai.com` |
| Writable volume | For `data/` (SQLite, cost state) and `reports/` |

---

## Local Development Setup

```bash
# 1. Clone the repository — the directory MUST be named "alerttriage"
git clone https://github.com/tbustenk/alerttriage.git alerttriage
cd alerttriage

# 2. Create and activate a virtual environment
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

# 3. Install in editable mode (includes all extras)
pip install -e .
pip install -r requirements-api.txt          # FastAPI + uvicorn + prometheus-client
pip install -r requirements-analytics.txt    # fpdf2 for PDF export (optional)

# 4. Create your local environment file
cp .env.example .env
# Edit .env — at minimum set:
#   ANTHROPIC_API_KEY=sk-ant-...
#   ALERTTRIAGE_API_KEYS=my-dev-key
#   ALERTTRIAGE_ANONYMIZE_SALT=local-dev-salt

# 5. Verify your setup with a dry-run health check
python scripts/run_api.py &
curl -H "X-API-Key: my-dev-key" http://localhost:8000/health
```

### Running the API Server

```bash
python scripts/run_api.py --host 0.0.0.0 --port 8000
```

The server uses uvicorn with hot-reload disabled by default. For development with auto-reload:

```bash
uvicorn alerttriage.src.api.app:app --reload --host 0.0.0.0 --port 8000
```

### Running the Dashboard

```bash
cd dashboard
pip install -r requirements.txt
python app.py
# Dashboard available at http://localhost:5000
```

### Running the CLI Batch Runner

```bash
python scripts/run_client.py --client-id acme-corp --dry-run
python scripts/run_client.py --client-id acme-corp --limit 50
```

---

## Environment Variables

### Required

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude models |
| `OPENAI_API_KEY` | OpenAI API key (only if using GPT fallback) |
| `ALERTTRIAGE_ANONYMIZE_SALT` | Salt for PII pseudonymisation. **Must be changed from the default in production.** |

### API and Auth

| Variable | Default | Description |
|----------|---------|-------------|
| `ALERTTRIAGE_API_KEYS` | *(unset — open access)* | Comma-separated list of valid API keys. If unset, the API runs in open-access mode (dev only). |
| `ALERTTRIAGE_API_RATE_LIMIT` | `60` | Maximum requests per minute per source IP |
| `ALERTTRIAGE_CORS_ORIGINS` | `*` | Comma-separated allowed CORS origins |

### Storage and Paths

| Variable | Default | Description |
|----------|---------|-------------|
| `ALERTTRIAGE_CONFIG_DIR` | `config/` | Path to YAML config directory |
| `ALERTTRIAGE_DATA_DIR` | `data/` | Root directory for per-client SQLite and cost state |
| `ALERTTRIAGE_REPORTS_DIR` | `reports/` | Output directory for generated reports |

### Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `ALERTTRIAGE_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `ALERTTRIAGE_JSON_LOGS` | `false` | Set to `true` for structured JSON output (required for most log aggregators) |

### Monitoring and Alerting

| Variable | Default | Description |
|----------|---------|-------------|
| `ALERTTRIAGE_ACCURACY_THRESHOLD` | `0.70` | Alert when per-client accuracy drops below this |
| `ALERTTRIAGE_LATENCY_THRESHOLD_MS` | `1000` | Alert when p95 latency exceeds this (ms) |
| `ALERTTRIAGE_SLACK_WEBHOOK_URL` | *(unset)* | Slack webhook for threshold breach notifications |
| `ALERTTRIAGE_ALERT_EMAIL_TO` | *(unset)* | Email address for threshold breach notifications |
| `ALERTTRIAGE_SMTP_HOST` | *(unset)* | SMTP server hostname |
| `ALERTTRIAGE_SMTP_PORT` | `587` | SMTP port |
| `ALERTTRIAGE_SMTP_TLS` | `true` | Use STARTTLS |
| `ALERTTRIAGE_SMTP_USER` | *(unset)* | SMTP username |
| `ALERTTRIAGE_SMTP_PASS` | *(unset)* | SMTP password |

---

## Docker Deployment

The repository ships a multi-stage Dockerfile and a `docker-compose.yml`.

### Quick Start (API only)

```bash
# 1. Copy and populate the env file
cp .env.example .env

# 2. Start the API
docker compose up api

# 3. Verify health
curl http://localhost:8000/health
```

### docker-compose Services

| Service | Profile | Description |
|---------|---------|-------------|
| `api` | *(always)* | FastAPI REST API on port 8000 |
| `triage` | `triage` | One-shot CLI batch runner |
| `postgres` | `postgres` | PostgreSQL (for future persistence extensions) |

### Running with PostgreSQL

```bash
docker compose --profile postgres up
```

### Running a One-Shot Batch

```bash
docker compose run --rm triage --client-id acme-corp
```

### Volume Mounts

| Container path | Purpose | Recommended host mount |
|----------------|---------|----------------------|
| `/data` | Per-client SQLite databases and cost state | Named volume or `/srv/alerttriage/data` |
| `/reports` | Generated analytics reports | Named volume or `/srv/alerttriage/reports` |
| `/app/alerttriage/config` | YAML config files (read-only) | `./config` (bind mount) |

### Production Docker Run (without Compose)

```bash
docker build -t alerttriage:2.0.0 --target production .

docker run -d \
  --name alerttriage-api \
  --restart unless-stopped \
  --env-file /etc/alerttriage/.env \
  -v alerttriage-data:/data \
  -v alerttriage-reports:/reports \
  -v /etc/alerttriage/config:/app/alerttriage/config:ro \
  -p 8000:8000 \
  --entrypoint python \
  alerttriage:2.0.0 \
  scripts/run_api.py --host 0.0.0.0 --port 8000
```

---

## Production Deployment Checklist

### Security

- [ ] `ALERTTRIAGE_API_KEYS` is set to one or more strong random keys (use `python -c "import secrets; print(secrets.token_hex(32))"`)
- [ ] `ALERTTRIAGE_ANONYMIZE_SALT` is set to a random value and is NOT the default `change-me-in-production`
- [ ] API is behind a TLS-terminating reverse proxy (nginx, Caddy, ALB)
- [ ] Rate limiting is configured (`ALERTTRIAGE_API_RATE_LIMIT`)
- [ ] CORS origins are restricted (`ALERTTRIAGE_CORS_ORIGINS`)
- [ ] Container runs as non-root user (`alerttriage` user is built into the image)

### Configuration

- [ ] `ALERTTRIAGE_JSON_LOGS=true` for log aggregation pipeline
- [ ] Cost limits are set in every client YAML (`cost_limits.daily_usd`, `cost_limits.monthly_usd`)
- [ ] `anonymize_salt` in `default_config.yaml` is set or overridden via env var
- [ ] `config/` directory is mounted read-only in the container

### Storage

- [ ] `/data` volume is on persistent storage (not ephemeral container storage)
- [ ] `/data` is included in backup schedule
- [ ] `/reports` has sufficient space for scheduled report accumulation

### Monitoring

- [ ] `/health` endpoint is configured as a container healthcheck
- [ ] Prometheus is scraping `/metrics`
- [ ] `ALERTTRIAGE_SLACK_WEBHOOK_URL` or `ALERTTRIAGE_ALERT_EMAIL_TO` is configured
- [ ] Log aggregation pipeline is receiving structured JSON logs

### Operations

- [ ] A process supervisor restarts the API on crash (`restart: unless-stopped` in Compose, or `Restart=always` in systemd)
- [ ] Deployment process includes `docker pull` + rolling restart (not in-place rebuild)
- [ ] At least one test client has been validated end-to-end with `dry_run=true`

---

## Kubernetes

AlertTriage runs well as a Deployment with a PersistentVolumeClaim for the data directory. A minimal example:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: alerttriage-api
spec:
  replicas: 1   # See scaling notes before running multiple replicas
  selector:
    matchLabels:
      app: alerttriage-api
  template:
    metadata:
      labels:
        app: alerttriage-api
    spec:
      containers:
        - name: api
          image: alerttriage:2.0.0
          command: ["python", "scripts/run_api.py"]
          args: ["--host", "0.0.0.0", "--port", "8000"]
          ports:
            - containerPort: 8000
          envFrom:
            - secretRef:
                name: alerttriage-secrets
          volumeMounts:
            - name: data
              mountPath: /data
            - name: config
              mountPath: /app/alerttriage/config
              readOnly: true
          livenessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 15
            periodSeconds: 30
          readinessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 5
            periodSeconds: 10
          resources:
            requests:
              cpu: "250m"
              memory: "256Mi"
            limits:
              cpu: "1000m"
              memory: "512Mi"
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: alerttriage-data
        - name: config
          configMap:
            name: alerttriage-config
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: alerttriage-data
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
---
apiVersion: v1
kind: Service
metadata:
  name: alerttriage-api
spec:
  selector:
    app: alerttriage-api
  ports:
    - port: 8000
      targetPort: 8000
```

For config files, mount a ConfigMap containing the YAML content:

```bash
kubectl create configmap alerttriage-config \
  --from-file=config/default_config.yaml \
  --from-file=config/features.yaml \
  --from-file=config/alert_types.yaml
```

For secrets:

```bash
kubectl create secret generic alerttriage-secrets \
  --from-literal=ANTHROPIC_API_KEY=sk-ant-... \
  --from-literal=ALERTTRIAGE_API_KEYS=key1,key2 \
  --from-literal=ALERTTRIAGE_ANONYMIZE_SALT=$(python -c "import secrets; print(secrets.token_hex(32))")
```

---

## Scaling Recommendations

### Single Instance (up to ~1,000 alerts/day)

Default configuration. One API process, SQLite storage, in-memory rate limiter.

- `concurrency.max_in_flight: 5` (default)
- One replica

### Medium Volume (~10,000 alerts/day)

- Increase `concurrency.max_in_flight` to 15-20
- Increase `ALERTTRIAGE_API_RATE_LIMIT` to 200+
- Schedule bulk batch jobs with `analyze_batch()` rather than individual API calls
- Use SSD-backed storage for `/data` to improve SQLite WAL performance

### High Volume (100,000+ alerts/day)

- Run multiple API replicas behind a load balancer (see horizontal scaling note below)
- Consider sharding clients across replicas (client `acme-corp` always routes to replica 1)
- Export feedback data to a read replica for analytics queries
- Tune SQLite: `PRAGMA wal_autocheckpoint = 1000`, `PRAGMA cache_size = -64000`

**Horizontal Scaling Note:** The in-memory result cache and rate limiter are per-process. Running multiple replicas means:
- The result cache (`GET /results/{alert_id}`) may miss across replicas — this is a soft miss, not a data loss.
- Rate limiting is per-process; effective limit is `ALERTTRIAGE_API_RATE_LIMIT × replica_count`.
- The feedback database is per-client and accessed by one replica at a time (use client-affinity load balancing or a shared NFS/EFS volume with `WAL` mode).

---

## Disaster Recovery

`data/<client_id>/` contains all stateful data per client:

| File | Content | Recovery strategy |
|------|---------|-------------------|
| `feedback.db` | Analyst verdicts, learning data | Restore from backup; or rebuild from SOAR export |
| `cost.json` | Daily/monthly AI spend totals | Lose at most one day's spend tracking; resets automatically |

Corrupt SQLite databases are automatically archived as `feedback.db.corrupt-<timestamp>` and a fresh empty database is created. The service keeps running. Rebuild state from a backup or from the upstream SOAR.

**Backup command:**

```bash
# Back up all client data
rsync -a /data/ /backup/alerttriage-data-$(date +%Y%m%d)/

# Or with Docker
docker run --rm \
  -v alerttriage-data:/data:ro \
  -v /backup:/backup \
  alpine tar czf /backup/alerttriage-$(date +%Y%m%d).tar.gz /data
```

**Config versioning:** The `POST /config/{client_id}/snapshot` endpoint creates a point-in-time YAML snapshot stored in `data/versions/`. Up to 20 snapshots are retained per client. Roll back with `POST /config/{client_id}/rollback/{version_id}`.
