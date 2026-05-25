# Elasticsearch / Kibana (ELK) — Setup Guide

AlertTriage integrates with the Kibana Detection Engine via the Elasticsearch
REST API (port 9200) and the Kibana API (port 5601). This guide covers API key
creation, basic-auth fallback, connector config, and connectivity testing.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Elasticsearch 7.13+ or 8.x | Detection Engine signals available from 7.13 |
| Kibana with Security enabled | Required for Detection Engine API |
| X-Pack Security | Required for API keys and RBAC |
| Network access | AlertTriage host must reach ES (9200) and Kibana (5601) |

---

## 1. Create an API Key (Recommended)

In Kibana: **Stack Management → Security → API Keys → Create API Key**

Name it `alerttriage-<client-id>` and assign these privileges:

```json
{
  "alerttriage": {
    "cluster": ["monitor"],
    "indices": [
      {
        "names": [".siem-signals-*", ".alerts-security.alerts-*"],
        "privileges": ["read", "write"]
      }
    ],
    "applications": [
      {
        "application": "kibana-.kibana",
        "privileges": ["feature_siem.all"],
        "resources": ["*"]
      }
    ]
  }
}
```

After creation, Kibana shows a base64 `id:key` string.  Set it as an env var:

```bash
export ALERTTRIAGE_<CLIENT_ID_UPPER>_API_KEY=base64string
# Example for client_id "acme-corp":
export ALERTTRIAGE_ACME_CORP_API_KEY=dGVzdDp0ZXN0...
```

---

## 2. Basic Auth (Alternative)

If API keys are not available, use basic auth with a dedicated service account:

1. Create a user in Kibana: **Stack Management → Security → Users → Create user**
2. Assign the built-in role `siem_analyst` (or a custom role with equivalent privileges).
3. Set `username` in the config file and the password as an env var:

```bash
export ALERTTRIAGE_<CLIENT_ID_UPPER>_ES_PASSWORD=your-password
```

> AlertTriage **never** stores passwords in YAML files.

---

## 3. Configure the Connector

Copy the template and edit:

```bash
cp config/siem_elk_template.yaml config/client_configs/acme-corp.yaml
```

Minimum required changes:

```yaml
client_id: acme-corp
display_name: Acme Corp

siem:
  type: elk
  host: elastic.acme-corp.internal   # ← your ES/Kibana host
  port: 9200
  kibana_port: 5601
  verify_ssl: true

  # Auth: API key via env var (no extra fields needed)
  # OR basic auth:
  # username: alerttriage-svc

  index: ".siem-signals-*"
  time_range: "now-15m"
```

---

## 4. Test Connectivity

```bash
python scripts/test_siem.py --config config/client_configs/acme-corp.yaml
```

Run all checks including field discovery and sample fetch:

```bash
python scripts/test_siem.py --config config/client_configs/acme-corp.yaml --all
```

Expected output:

```
AlertTriage SIEM Connectivity Test — acme-corp.yaml

  ·  SIEM type  : ELK
  ·  Host       : elastic.acme-corp.internal
  ...

1. Config validation
  ✓  Config looks good — required keys present, auth env var found

2. Connectivity & auth
  ✓  Health check passed (38 ms)

3. Field discovery (schema)
  ✓  Discovered 312 fields in '.siem-signals-*' (95 ms)

4. Alert fetch (sample — limit 5)
  ✓  Fetched 5 alert(s) (143 ms)

Result
  All checks passed. AlertTriage can connect to your SIEM.
```

---

## 5. Run AlertTriage

```bash
python scripts/run_client.py --client-id acme-corp
```

---

## Index Patterns

AlertTriage works with both the legacy signals index and the newer alerts index:

| Stack version | Index pattern |
|---|---|
| 7.13 – 8.3 | `.siem-signals-*` (default) |
| 8.4+ | `.alerts-security.alerts-*` |

Update `siem.index` in your config to match your version:

```yaml
siem:
  index: ".alerts-security.alerts-*"   # Elastic 8.4+
```

---

## Time Range

`time_range` uses [Elasticsearch date math](https://www.elastic.co/guide/en/elasticsearch/reference/current/common-options.html#date-math):

| Value | Meaning |
|---|---|
| `now-15m` | Last 15 minutes (default) |
| `now-1h` | Last hour |
| `now-1d` | Last day |
| `now-7d/d` | Last 7 days, rounded to midnight |

Override per-run via the API:

```python
alerts = await connector.fetch_alerts(time_range="now-1h")
```

---

## Write-Back: How Verdicts Are Recorded

AlertTriage calls `POST /api/detection_engine/signals/status` on Kibana:

- **False positive** → signal status set to `closed`
- **True positive / suspicious** → signal status set to `acknowledged`

A comment is written to the signal with the verdict and reasoning.

---

## Elasticsearch 8.x: TLS Without Verify

Elastic 8.x enables TLS with a self-signed certificate by default.  Options:

**Option A — Trust the cluster CA (recommended):**

```bash
# Extract the CA cert from the Elasticsearch container
docker cp es01:/usr/share/elasticsearch/config/certs/ca/ca.crt ./es-ca.crt
```

Then set `REQUESTS_CA_BUNDLE=./es-ca.crt` or configure httpx with the cert path
(advanced: add `ssl_ca_cert` to your connector config via a custom subclass).

**Option B — Disable verify (dev/test only):**

```yaml
siem:
  verify_ssl: false
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Config invalid: needs auth` | No API key or basic auth configured | Set the env var (see Step 1) |
| `Health check returned False` | Cluster unhealthy or wrong host | Check ES logs; try `curl http://host:9200/_cluster/health` |
| `SSL verify failed` | Self-signed cert | See TLS section above; or `verify_ssl: false` in dev |
| `403 Forbidden on fetch` | Missing index read privilege | Add `.siem-signals-*` read to the API key |
| `403 Forbidden on send_result` | Missing Kibana SIEM privilege | Add `feature_siem.all` to the API key |
| `No alerts returned` | Index pattern mismatch | Check `siem.index` matches your stack version |
| Empty field list | Index has no data yet | Run a detection rule to generate signals first |

---

## API Key Rotation

1. Create a new API key in Kibana.
2. Update the env var on all hosts running AlertTriage.
3. Restart AlertTriage.
4. Invalidate the old key: **Stack Management → Security → API Keys → Invalidate**.
