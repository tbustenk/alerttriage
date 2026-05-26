# AlertTriage v2 — Security Guide

## Table of Contents

- [Authentication and API Keys](#authentication-and-api-keys)
- [Webhook HMAC Verification](#webhook-hmac-verification)
- [Rate Limiting Configuration](#rate-limiting-configuration)
- [Security Headers](#security-headers)
- [Secrets Management](#secrets-management)
- [Network Hardening](#network-hardening)
- [Audit Logging](#audit-logging)
- [Compliance Notes](#compliance-notes)

---

## Authentication and API Keys

### How It Works

All API endpoints except `GET /health` and `GET /metrics` require an `X-API-Key` header. Keys are loaded at process startup from the `ALERTTRIAGE_API_KEYS` environment variable (comma-separated list of raw key strings). Key comparisons use `secrets.compare_digest` to prevent timing attacks.

```
ALERTTRIAGE_API_KEYS=key1,key2,key3
```

```http
GET /stats/acme-corp HTTP/1.1
X-API-Key: key1
```

If `ALERTTRIAGE_API_KEYS` is not set, the API runs in open-access mode. A `WARNING` is emitted to the log at startup. **Never run in open-access mode in production.**

### Generating Secure Keys

```bash
# Generate a strong 256-bit key
python -c "import secrets; print(secrets.token_hex(32))"
# e.g.: 7f3a8c2b9d1e4f6a0b3c5d7e9f1a2b4c6d8e0f1a3b5c7d9e1f2a4b6c8d0e1f3a
```

### Key Rotation

Keys are loaded once at startup. To rotate:

1. Add the new key alongside the old key in `ALERTTRIAGE_API_KEYS`:
   `ALERTTRIAGE_API_KEYS=old-key,new-key`
2. Deploy the updated env var and restart the API.
3. Update all callers to use the new key.
4. Remove the old key from `ALERTTRIAGE_API_KEYS` and restart again.

### Role-Based Access

AlertTriage does not implement role-based access control — all valid API keys have full access. If you need differentiated access (e.g., read-only for dashboards, write for SIEM integrations), deploy separate API instances with separate key sets, or place a reverse proxy with path-based ACLs in front of the API.

---

## Webhook HMAC Verification

When a `secret` is set on a webhook registration, every outbound delivery is signed with HMAC-SHA256. The signature is sent in the `X-AlertTriage-Signature` header:

```
X-AlertTriage-Signature: sha256=<64-hex-character-digest>
```

### Signing Algorithm

```
signature = HMAC-SHA256(key=secret.encode('utf-8'), msg=payload_bytes)
header = f"sha256={signature.hexdigest()}"
```

The `payload_bytes` is the raw UTF-8 JSON body of the webhook event as received by the endpoint.

### Verification (Python)

```python
import hashlib
import hmac

def verify_webhook_signature(
    payload: bytes,
    secret: str,
    signature_header: str,
) -> bool:
    """Return True if the signature is valid."""
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature_header, expected)

# In a Flask receiver:
from flask import request

@app.route("/alerttriage-webhook", methods=["POST"])
def receive():
    payload = request.get_data()  # raw bytes — do NOT decode first
    sig = request.headers.get("X-AlertTriage-Signature", "")
    if not verify_webhook_signature(payload, MY_SECRET, sig):
        return "Forbidden", 403
    event = request.json
    process(event)
    return "OK", 200
```

**Always use `hmac.compare_digest`** — do not use `==` for signature comparison, as it is vulnerable to timing attacks.

### Choosing a Strong Webhook Secret

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Use a different secret for each registered webhook endpoint.

---

## Rate Limiting Configuration

The rate limiter is a per-source-IP sliding window implemented in Python without external dependencies.

### Configuration

```bash
# Requests per minute (default: 60)
ALERTTRIAGE_API_RATE_LIMIT=60
```

The limit applies per unique source IP. When behind a reverse proxy, the limiter uses the first address in `X-Forwarded-For`. Ensure your proxy sets this header correctly and strips untrusted values from incoming requests.

### Behaviour on Limit Exceeded

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 60

{"detail": "Rate limit exceeded: 60 requests/minute allowed"}
```

### Hardening the Rate Limiter

The in-memory limiter is per-process and per-IP only. For stronger DoS protection:

1. Place a reverse proxy (nginx, Caddy, AWS ALB) in front of the API and apply rate limiting there.
2. For nginx:
   ```nginx
   limit_req_zone $binary_remote_addr zone=alerttriage:10m rate=60r/m;
   location / {
       limit_req zone=alerttriage burst=10 nodelay;
       proxy_pass http://localhost:8000;
   }
   ```
3. For Kubernetes, use an Ingress annotation or a service mesh policy.

---

## Security Headers

The FastAPI application sets CORS headers via middleware. Additional security headers should be applied at the reverse proxy layer.

### CORS Configuration

```bash
# Comma-separated list of allowed origins (default: * — allow all)
# In production, restrict to your dashboard and SOAR origins:
ALERTTRIAGE_CORS_ORIGINS=https://soc.acme.com,https://soar.acme.com
```

**Warning:** The default `*` allows any origin. Always restrict in production.

### Recommended Reverse Proxy Headers (nginx)

```nginx
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
add_header X-Content-Type-Options "nosniff" always;
add_header X-Frame-Options "DENY" always;
add_header X-XSS-Protection "1; mode=block" always;
add_header Referrer-Policy "strict-origin-when-cross-origin" always;
add_header Content-Security-Policy "default-src 'self'" always;
```

### Recommended Reverse Proxy Headers (Caddy)

```caddy
header {
    Strict-Transport-Security "max-age=31536000; includeSubDomains"
    X-Content-Type-Options nosniff
    X-Frame-Options DENY
    Referrer-Policy strict-origin-when-cross-origin
}
```

---

## Secrets Management

### Environment Variables (Minimum)

Store all secrets in environment variables, never in YAML files or source code.

| Secret | Variable |
|--------|---------|
| Anthropic API key | `ANTHROPIC_API_KEY` |
| OpenAI API key | `OPENAI_API_KEY` |
| AlertTriage API keys | `ALERTTRIAGE_API_KEYS` |
| PII anonymisation salt | `ALERTTRIAGE_ANONYMIZE_SALT` |
| Per-client SIEM credentials | `ALERTTRIAGE_<CLIENT_ID_UPPER>_API_KEY` |
| SMTP password | `ALERTTRIAGE_SMTP_PASS` |
| Webhook signing secrets | Stored in `data/webhooks.json` (not an env var — see below) |

### Webhook Secrets in `data/webhooks.json`

Webhook registration secrets are stored in `data/webhooks.json`. This file is on a persistent volume. Ensure the volume is:

- Owned by the `alerttriage` user only (mode `600` or `640`)
- Not accessible from the host filesystem by other users
- Excluded from version control (the `data/` directory should be in `.gitignore`)

```bash
chmod 640 /data/webhooks.json
chown alerttriage:alerttriage /data/webhooks.json
```

### AWS Secrets Manager

For AWS deployments, use the AWS SDK to load secrets at startup:

```python
import boto3
import json
import os

def load_secrets(secret_name: str) -> None:
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_name)
    secrets = json.loads(response["SecretString"])
    for key, value in secrets.items():
        os.environ[key] = value

# Call before importing alerttriage modules:
load_secrets("alerttriage/production")
```

### HashiCorp Vault

For Vault deployments, use `vault agent` to populate the `.env` file:

```hcl
# vault-agent.hcl
template {
  destination = "/etc/alerttriage/.env"
  contents = <<EOT
ANTHROPIC_API_KEY={{ with secret "secret/alerttriage/anthropic" }}{{ .Data.data.api_key }}{{ end }}
ALERTTRIAGE_API_KEYS={{ with secret "secret/alerttriage/api-keys" }}{{ .Data.data.keys }}{{ end }}
ALERTTRIAGE_ANONYMIZE_SALT={{ with secret "secret/alerttriage/config" }}{{ .Data.data.salt }}{{ end }}
EOT
}
```

### Kubernetes Secrets

```bash
kubectl create secret generic alerttriage-secrets \
  --from-literal=ANTHROPIC_API_KEY=sk-ant-... \
  --from-literal=ALERTTRIAGE_API_KEYS=key1,key2 \
  --from-literal=ALERTTRIAGE_ANONYMIZE_SALT=$(python -c "import secrets; print(secrets.token_hex(32))")
```

Reference in the Deployment:

```yaml
envFrom:
  - secretRef:
      name: alerttriage-secrets
```

---

## Network Hardening

### TLS Termination

AlertTriage does not handle TLS natively. Terminate TLS at a reverse proxy.

**nginx example:**

```nginx
server {
    listen 443 ssl http2;
    server_name api.alerttriage.acme.com;

    ssl_certificate /etc/ssl/certs/alerttriage.crt;
    ssl_certificate_key /etc/ssl/private/alerttriage.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:...;
    ssl_prefer_server_ciphers off;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}

# Redirect HTTP to HTTPS
server {
    listen 80;
    server_name api.alerttriage.acme.com;
    return 301 https://$host$request_uri;
}
```

### Network Isolation

- The API process only needs outbound access to `api.anthropic.com:443` and optionally `api.openai.com:443`.
- Bind the API to `127.0.0.1:8000` and expose it only through the reverse proxy:
  ```bash
  python scripts/run_api.py --host 127.0.0.1 --port 8000
  ```
- In Docker, do not expose port 8000 directly; use a reverse proxy container or an internal Docker network.
- Restrict `/metrics` to your Prometheus server's IP at the reverse proxy layer if you do not want it publicly accessible.

### Firewall Rules

| Direction | Protocol | Port | Source | Destination | Reason |
|-----------|---------|------|--------|-------------|--------|
| Inbound | TCP | 443 | Any | API host | HTTPS to reverse proxy |
| Outbound | TCP | 443 | API host | `api.anthropic.com` | Claude API |
| Outbound | TCP | 443 | API host | `api.openai.com` | GPT API (if used) |
| Outbound | TCP | 443 | API host | Webhook URLs | Webhook delivery |
| Outbound | TCP | 587 | API host | SMTP server | Report emails |

---

## Audit Logging

AlertTriage emits structured log events for all security-relevant operations. Enable JSON logging for machine-parseable audit trails:

```bash
ALERTTRIAGE_JSON_LOGS=true
```

### Security-Relevant Log Events

| Event | Level | Fields logged |
|-------|-------|--------------|
| `analyzing_alert` | INFO | `alert_id`, `client`, `rule`, `severity` |
| `analysis_complete` | INFO | `alert_id`, `verdict`, `confidence`, `cost_usd`, `latency_ms` |
| `feedback_recorded` | INFO | `alert_id`, `client` |
| `feedback_recorded_batch` | INFO | `client`, `count` |
| `context_stored` | INFO | `client`, `context_type` |
| `config_changed` | INFO | `path` |
| `api_open_access` | WARNING | `detail` |
| `feedback_db_corrupt` | ERROR | `client`, `db_path`, `backup_path`, `error` |
| `cost_limit_hit` | ERROR | `client`, `limit_type`, `limit`, `current` |

**Note:** API key values are never logged. The key is used only for authentication and is not forwarded to any component.

### Log Retention and Forwarding

For compliance, ship logs to a SIEM or log aggregation platform (ELK, Splunk, CloudWatch):

```bash
# Forward to Splunk HEC
docker logs -f alerttriage-api | \
  curl -s -X POST "https://splunk.acme.com:8088/services/collector/event" \
  -H "Authorization: Splunk $SPLUNK_HEC_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @-
```

Or configure the Docker logging driver:

```yaml
services:
  api:
    logging:
      driver: fluentd
      options:
        fluentd-address: localhost:24224
        tag: alerttriage.api
```

---

## Compliance Notes

AlertTriage's design addresses several SOC 2 Type II control categories. This is not legal advice — consult your compliance team.

### CC6 — Logical and Physical Access Controls

| Control | AlertTriage implementation |
|---------|--------------------------|
| CC6.1 Access restricted by valid credentials | `X-API-Key` authentication on all write endpoints; `secrets.compare_digest` prevents timing attacks |
| CC6.2 Least privilege | Keys provide full API access — segment with separate instances if role-based access is needed |
| CC6.6 Transmission encryption | TLS at reverse proxy; all AI calls use HTTPS |

### CC7 — System Operations

| Control | AlertTriage implementation |
|---------|--------------------------|
| CC7.1 Detection of security events | Prometheus metrics + threshold alerting via `HealthMonitor` |
| CC7.2 Security incidents monitored | Structured audit logs for all analysis and feedback operations |
| CC7.4 Security incidents remediation | Config rollback via `POST /config/{client_id}/rollback/{version_id}`; corrupt DB auto-recovery |

### CC8 — Change Management

| Control | AlertTriage implementation |
|---------|--------------------------|
| CC8.1 Changes authorised and tracked | Config versioning: every change snapshotted before applying; rollback supported |

### PI1 — Processing Integrity

| Control | AlertTriage implementation |
|---------|--------------------------|
| PI1.2 Inputs complete and accurate | Pydantic validation on all request models; errors rejected at ingestion |
| PI1.4 Outputs complete and accurate | `AnalysisResult` includes full reasoning, confidence, and cost metadata |

### PII Handling

AlertTriage pseudonymises PII before sending data to AI backends:

- IP addresses → `<IP:8-char-hash>`
- Email addresses → `<EMAIL:8-char-hash>`
- Credit card numbers → `<CC:8-char-hash>`
- SSNs → `<SSN:8-char-hash>`
- API key patterns → `<APIKEY:8-char-hash>`

Pseudonyms are deterministic within a salt (set via `ALERTTRIAGE_ANONYMIZE_SALT`). The reverse map (hash → original value) exists only in RAM for the duration of a single analysis call and is never persisted or logged.

**Important:** Change `ALERTTRIAGE_ANONYMIZE_SALT` from the default before going to production. A predictable salt allows reverse-engineering of pseudonyms via brute force.
