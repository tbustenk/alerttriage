# Deployment

This guide covers running AlertTriage v2 in production. For day-1
onboarding of a new client see [CLIENT_SETUP.md](CLIENT_SETUP.md); for the
end-to-end design see [ARCHITECTURE.md](ARCHITECTURE.md).

## Prerequisites

* Python 3.11+
* Outbound HTTPS to `api.anthropic.com` and/or `api.openai.com`.
* Outbound network access to your SIEM (Splunk REST 8089, Elasticsearch 9200, Kibana 5601, or your webhook).
* A writable persistent volume mounted at the path used for `data_dir` (default `data/`).
* (Recommended) A separate readable volume for `reports_dir`.

## Required environment variables

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Required if any client uses a Claude model. |
| `OPENAI_API_KEY` | Required if any client uses a GPT model. |
| `ALERTTRIAGE_ANONYMIZE_SALT` | **Change from the default in production.** Drives PII pseudonyms. |
| `ALERTTRIAGE_<CLIENT_ID_UPPER>_API_KEY` | Per-client SIEM credential; never inline in YAML. Hyphens in client IDs become underscores (`acme-corp` → `ALERTTRIAGE_ACME_CORP_API_KEY`). |
| `ALERTTRIAGE_LOG_LEVEL` | Optional, overrides `default_config.yaml`. |
| `ALERTTRIAGE_JSON_LOGS` | `true`/`1` to switch to JSON logs for log aggregators. |
| `ALERTTRIAGE_DATA_DIR` | Override the data directory if you can't use the default. |

## Production `default_config.yaml`

The shipped defaults are conservative. For a real deployment:

```yaml
log_level: INFO
json_logs: true                       # required by most log aggregators

data_dir: /var/lib/alerttriage
reports_dir: /var/lib/alerttriage/reports

anonymize_salt: "${ALERTTRIAGE_ANONYMIZE_SALT}"   # never commit a real value

concurrency:
  max_in_flight: 10                   # tune up for high-volume SIEMs

retry:
  max_attempts: 5                     # 4xx never retries; 5xx and rate-limit do
  initial_delay_sec: 0.5
  max_delay_sec: 60.0
  factor: 2.0
  jitter: true                        # avoids thundering-herd retries

learning:
  fp_rate_threshold: 0.50
  min_sample_size: 5
  max_hints: 10
  cache_ttl_sec: 300                  # keeps Anthropic prompt cache warm
```

## Operating models

### One-shot batch (cron / systemd timer)

```bash
* */15 * * * /usr/local/bin/alerttriage-run --client-id acme-corp --limit 200
```

The CLI exits with:
* `0` — success (even when there are no alerts to process).
* `1` — SIEM unreachable on health check.
* `2` — invalid configuration (`ConfigError`).
* `3` — client cost limit exceeded mid-run.

### Long-lived daemon

Wrap `python scripts/run_client.py` in a supervisor (`systemd`, `runit`,
Kubernetes Job → CronJob). Each invocation is independent — there is no
hidden mutable state across runs apart from `data/<client_id>/`.

## Docker outline

```Dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -e .
ENV ALERTTRIAGE_DATA_DIR=/data
VOLUME ["/data"]
ENTRYPOINT ["python", "scripts/run_client.py"]
```

Run with:

```bash
docker run --rm \
  -v /srv/alerttriage:/data \
  -e ANTHROPIC_API_KEY -e OPENAI_API_KEY \
  -e ALERTTRIAGE_ANONYMIZE_SALT \
  -e ALERTTRIAGE_ACME_CORP_API_KEY \
  alerttriage --client-id acme-corp --limit 100
```

## Cost discipline

Always set both `daily_usd` and `monthly_usd` in `cost_limits` per client.
The controller writes spend atomically (`tmp + rename`) and rolls
counters over automatically when the calendar day or month changes —
there is no cron job to forget.

Inspect spend without invoking the analyzer:

```python
from alerttriage.config.config_manager import ConfigManager
from alerttriage.src.cost_controller import CostController

CostController(ConfigManager()).get_spend("acme-corp")
# {'daily_usd': 1.27, 'monthly_usd': 38.94, 'total_usd': 412.05}
```

## Observability

* **Structured logs**: `structlog` emits one line per state transition.
  Key events to alert on:
  * `cost_limit_hit` — client budget exhausted.
  * `siem_unreachable` — SIEM health check failed.
  * `backend_failed` — single backend failure (recoverable via fallback).
  * `feedback_db_corrupt` — store had to be re-initialised.
* **Reports**: `ReportGenerator` writes HTML/JSON snapshots to
  `reports_dir`. Schedule them daily for SOC reporting.

## Rolling out a new model

1. Add the model spec to `config/models.yaml` (provider, model name, max tokens, temperature).
2. If pricing differs, update the `_PRICING` dict in the matching backend.
3. Set it as `model:` for one client, keep the previous model in `model_fallbacks:` for a week.
4. Once the new model proves itself, remove the fallback.

## Disaster recovery

`data/<client_id>/` holds everything stateful for a client:
* `feedback.db` — analyst feedback (SQLite).
* `cost.json` — running spend.

Back up the directory; restoring is a straight copy. Corrupt feedback DBs
are auto-archived (`feedback.corrupt-<ts>`) on next startup so you can
investigate offline.
