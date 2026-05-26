# AlertTriage

[![Tests](https://github.com/tbustenk/alerttriage/actions/workflows/tests.yml/badge.svg)](https://github.com/tbustenk/alerttriage/actions/workflows/tests.yml)
[![Coverage](https://img.shields.io/badge/coverage-80%25-brightgreen)](#)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

**AI-powered alert triage that gets smarter with every analyst decision.**

AlertTriage connects to your SIEM, runs each alert through Claude or GPT, and returns a verdict with confidence score and recommended actions — in under 200 ms. Every time an analyst corrects or confirms the AI, that signal is fed back into the next analysis. No fine-tuning. No embeddings. No infrastructure changes.

---

## What it does

```
SIEM alert → PII scrubbed → AI triage → verdict + confidence → SIEM
                                ↑
                     analyst feedback loop
```

Alerts come in. Verdicts go out. Analyst corrections make the next round smarter. Over time, false-positive rates fall and mean-time-to-triage drops.

---

## Notable features

### AI triage
- Routes alerts to **Claude** (Sonnet / Opus / Haiku) or **GPT** (4o / 4o-mini) — configurable per client
- Returns verdict, confidence score, risk factors, and recommended actions
- **Prompt caching** on Claude: repeated system prompts billed at ~10% of normal input rate

### Data protection
- **PII anonymisation** before any data reaches an AI provider — IPs, usernames, emails, card numbers, SSNs, and API keys are stripped or pseudonymised with a per-client salt
- Configurable **request size limits** and **security headers** (CSP, HSTS, X-Frame-Options)
- **Audit log** on every API call: who, what, when, outcome
- **Secrets management**: env vars → AWS Secrets Manager → HashiCorp Vault, in priority order

### Learning loop
- Analyst verdicts (true positive / false positive / escalated / closed) are stored per client in SQLite
- Learning engine builds **prompt hints** from verdict history and prepends them to future analyses
- Accuracy and false-positive rate trend over time as the system learns each client's environment

### Analytics & ROI
- **Weekly summaries**: throughput, accuracy, hours saved, labor value, AI cost, net ROI
- **Alert-type breakdown**: which rules generate the most false positives
- **False-positive pattern analysis** with recommendations
- **Scheduled reports** delivered by email (HTML, CSV, JSON, PDF)
- All figures available via REST API or the included web dashboard

### Integrations
| Connector | Details |
|---|---|
| Splunk Enterprise Security | Fetch notable events; write verdict fields back |
| Elasticsearch / ELK | Query indices; update documents with triage results |
| Generic HTTPS webhook | Outbound signed events for any downstream system |
| REST API | Full programmatic access — analyze, feedback, stats, config |

### Enterprise operations
- **Multi-tenant**: isolated config, data, and cost state per client
- **Cost control**: per-client daily and monthly hard limits, tracked across restarts
- **Outbound webhooks**: sign payloads with HMAC-SHA256; retry with exponential backoff
- **Hot-reload**: config changes take effect without a restart
- **Config versioning**: snapshot and rollback any client config
- **Rate limiting**: sliding-window per-IP with configurable RPM

---

## Quick start

```bash
# 1. Install
git clone https://github.com/tbustenk/alerttriage.git
cd alerttriage
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# 2. Set credentials
export ANTHROPIC_API_KEY=sk-ant-…           # or OPENAI_API_KEY
export ALERTTRIAGE_ANONYMIZE_SALT=$(openssl rand -hex 16)

# 3. Onboard a client
python scripts/init_client.py

# 4. Dry run — no AI cost, no SIEM writes
python scripts/run_client.py --client-id <id> --limit 10 --dry-run

# 5. Run live triage
python scripts/run_client.py --client-id <id> --limit 100
```

Or run as an API service:

```bash
uvicorn alerttriage.src.api.app:app --host 0.0.0.0 --port 8000
```

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for Docker, environment variables, and production checklist.

---

## Documentation

| Guide | Contents |
|---|---|
| [Architecture](docs/ARCHITECTURE.md) | Data flow, on-disk layout, feedback loop, multi-tenancy |
| [Deployment](docs/DEPLOYMENT.md) | Docker, env vars, production checklist, Kubernetes, DR |
| [API Reference](docs/API.md) | Every endpoint with request/response examples |
| [Client Setup](docs/CLIENT_SETUP.md) | Step-by-step onboarding, config fields, credential rotation |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | Common errors, DB recovery, SIEM connectivity |
| [Performance](docs/PERFORMANCE.md) | SQLite tuning, caching, benchmark targets, scaling tiers |
| [Security](docs/SECURITY.md) | Auth, HMAC signing, secrets management, compliance controls |
| [Contributing](CONTRIBUTING.md) | Dev setup, PR checklist, architecture decisions |

---

## License

[MIT](LICENSE)
