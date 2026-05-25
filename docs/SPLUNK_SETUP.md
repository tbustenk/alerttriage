# Splunk Enterprise Security — Setup Guide

AlertTriage integrates with Splunk ES via the Splunk REST API (port 8089).
This guide walks through creating a service account, generating a bearer
token, and wiring the connector into your client config.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Splunk Enterprise Security 6.x+ | Community Edition is **not** supported (no ES) |
| REST API enabled | Enabled by default on port 8089 |
| Network access | The AlertTriage host must reach `splunk-host:8089` |

---

## 1. Create a Service Account

In Splunk Web: **Settings → Users & Authentication → Users → New User**

| Field | Value |
|---|---|
| Username | `alerttriage-svc` |
| Role | `ess_analyst` (minimum read access to notables) |
| Role (for write-back) | `ess_analyst` + `can_delete` or a custom role |

> **Least-privilege tip:** Create a custom role with only these capabilities:
> `search`, `edit_notable_events`, `schedule_search`.

---

## 2. Generate a Bearer Token

1. Log in as the service account (or as an admin on its behalf).
2. Go to **Settings → Users → alerttriage-svc → Edit → Tokens**.
3. Click **New Token** → give it an expiry that matches your rotation policy.
4. Copy the token — you will **not** be able to see it again.

Set it as an environment variable on the host running AlertTriage:

```bash
export ALERTTRIAGE_<CLIENT_ID_UPPER>_API_KEY=eyJ...
# Example for client_id "acme-corp":
export ALERTTRIAGE_ACME_CORP_API_KEY=eyJ...
```

> AlertTriage **never** stores tokens in YAML files.  If a token appears in
> a config file it is ignored and a warning is logged.

---

## 3. Configure the Connector

Copy the template and edit:

```bash
cp config/siem_splunk_template.yaml config/client_configs/acme-corp.yaml
```

Minimum required changes:

```yaml
client_id: acme-corp
display_name: Acme Corp

siem:
  type: splunk
  host: splunk.acme-corp.internal   # ← your Splunk host
  port: 8089
  verify_ssl: true

  # Customise the SPL if your notables live in a non-standard index
  search_query: "| inputlookup notable | where status=0"
  earliest_time: "-15m"
```

---

## 4. Test Connectivity

```bash
python scripts/test_siem.py --config config/client_configs/acme-corp.yaml
```

Run with all checks (health + field discovery + sample fetch):

```bash
python scripts/test_siem.py --config config/client_configs/acme-corp.yaml --all
```

Expected output:

```
AlertTriage SIEM Connectivity Test — acme-corp.yaml

  ·  SIEM type  : SPLUNK
  ·  Host       : splunk.acme-corp.internal
  ...

1. Config validation
  ✓  Config looks good — required keys present, auth env var found

2. Connectivity & auth
  ✓  Health check passed (43 ms)

3. Field discovery (schema)
  ✓  Discovered 84 fields in 'notable' (210 ms)

4. Alert fetch (sample — limit 5)
  ✓  Fetched 5 alert(s) (185 ms)

Result
  All checks passed. AlertTriage can connect to your SIEM.
```

---

## 5. Run AlertTriage

```bash
python scripts/run_client.py --client-id acme-corp
```

---

## Customising the SPL Query

The default query fetches all open notables:

```spl
| inputlookup notable | where status=0
```

Common customisations:

```spl
# Only high/critical urgency
| inputlookup notable | where status=0 AND (urgency="high" OR urgency="critical")

# Specific correlation search
| inputlookup notable | where status=0 AND rule_name="Brute Force Access Behavior Detected"

# Last 30 minutes only (override earliest_time in config instead)
| inputlookup notable | where status=0 AND _time > relative_time(now(), "-30m@m")
```

Set your custom SPL in the config:

```yaml
siem:
  search_query: "| inputlookup notable | where status=0 AND urgency!='low'"
  earliest_time: "-30m"
```

---

## Write-Back: How Verdicts Are Recorded

When AlertTriage completes triage, it calls the Splunk Notable Update API:

- **True positive** → `status=1` (assigned) with an AI comment
- **False positive / benign** → `status=4` (resolved) with an AI comment

The comment format:

```
[AlertTriage AI] Verdict: false_positive (confidence: 94%)

<one-line summary>

Reasoning: <up to 500 characters of reasoning>
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Config invalid: No bearer token` | Env var not set | `export ALERTTRIAGE_<ID>_API_KEY=...` |
| `Health check returned False` | Wrong host/port | Verify `host` and `port` in config |
| `Connection refused` | Splunk REST not listening | Check Splunk is running; try `curl https://host:8089/services/server/info -k` |
| `SSL verify failed` | Self-signed cert | Set `verify_ssl: false` in dev (use a real cert in prod) |
| `No alerts returned` | SPL query too restrictive | Try `| inputlookup notable | head 10` to test |
| Notable status not updating | Service account missing `edit_notable_events` | Add capability to the role |

---

## Token Rotation

1. Generate a new token in Splunk Web.
2. Update the env var on all hosts running AlertTriage.
3. Restart the AlertTriage process (no config file change needed).
4. Revoke the old token.
