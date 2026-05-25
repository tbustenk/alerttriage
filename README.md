# AlertTriage v2

[![CI](https://github.com/tbustenk/alerttriage/actions/workflows/ci.yml/badge.svg)](https://github.com/tbustenk/alerttriage/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Production-grade AI alert triage. Connects to your SIEM, anonymises every
alert before it touches an AI model, routes it through Claude or GPT, and
writes the verdict back. Analyst decisions feed a per-client feedback loop
that prepends learning hints to future system prompts — no fine-tuning,
no embeddings.

---

## Features

| Feature | Details |
|---|---|
| Multi-model AI | Claude (Sonnet / Opus / Haiku) and GPT (4o / 4o-mini); per-client selection with automatic fallback |
| SIEM connectors | Splunk Enterprise Security, Elasticsearch/ELK, generic HTTPS webhook |
| PII anonymisation | IPs, usernames, emails, card numbers, SSNs, and API keys stripped before any data reaches an AI provider |
| Feedback loop | Analyst verdicts → SQLite → cached prompt hints (see [docs/FEEDBACK.md](docs/FEEDBACK.md)) |
| Production hardening | Atomic cost-state writes, daily/monthly rollover, retry-with-backoff, rate-limit awareness, graceful corrupt-DB recovery |
| Cost control | Per-client daily and monthly hard limits; spend tracked across restarts |
| Reporting | HTML and JSON reports (accuracy, cost, verdict breakdown) |
| Multi-tenant | Isolated config, data, and cost state per client |
| Prompt caching | Claude backend uses Anthropic prompt caching; repeated system prompts billed at ~10% of input rate |

---

## Quick start

```bash
# 1. Install
git clone https://github.com/tbustenk/alerttriage.git
cd alerttriage
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# 2. Configure credentials
export ANTHROPIC_API_KEY=sk-ant-…           # or OPENAI_API_KEY
export ALERTTRIAGE_ANONYMIZE_SALT=$(openssl rand -hex 16)

# 3. Onboard a client (interactive wizard)
python scripts/init_client.py

# 4. Sanity-check connectivity (no AI cost, no SIEM writes)
python scripts/run_client.py --client-id <id> --limit 10 --dry-run

# 5. Run a real triage batch
python scripts/run_client.py --client-id <id> --limit 100
```

Tests run offline with no live keys:

```bash
pytest tests/ -v
pytest tests/ --cov=alerttriage --cov-report=term-missing
```

---

## Documentation

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How alerts flow through the system, on-disk layout, multi-tenancy, extension points |
| [docs/CLIENT_SETUP.md](docs/CLIENT_SETUP.md) | Onboarding walkthrough, config reference, credential rotation, cost management |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Production checklist, environment variables, Docker, observability, DR |
| [docs/FEEDBACK.md](docs/FEEDBACK.md) | How analyst verdicts shape future analyses |
| [docs/API.md](docs/API.md) | Class and method reference |

---

## Project layout

The on-disk layout is **flat** (`config/`, `src/`, `scripts/`, `tests/`
all at the repo root). Imports nevertheless use the dotted form
`alerttriage.config.*` / `alerttriage.src.*`, so:

* `pip install -e .` is the supported install path.
* When running scripts directly, the repo directory must be named
  `alerttriage` so the parent shows up on `sys.path` as the package root.
  `scripts/run_client.py` handles this for you.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#on-disk-layout) for the
full tree.

---

## Adding a new AI model

1. Add a spec to `config/models.yaml` (`provider`, `model_name`, `max_tokens`, `temperature`).
2. If it's a new provider, create `src/models/<provider>_backend.py` with
   `async def analyze(alert, *, system_prompt, retry_config) -> AnalysisResult`.
3. Register it in `ModelRouter._load_backends()`.
4. Set it as `model:` in a client config.

---

## Adding a new SIEM connector

1. Create `src/integrations/<name>_connector.py`, subclass `SIEMConnector`.
2. Implement `fetch_alerts()`, `send_result()`, `health_check()` (and optionally `close()`).
3. Register it in `scripts/run_client.py`'s `_build_connector()` factory.
4. Add a `type: <name>` example to `config/client_configs/template.yaml`.

---

## Contributing

We welcome bug reports, feature requests, and pull requests. Please read
[CONTRIBUTING.md](CONTRIBUTING.md) before opening a PR.

## Security

For security vulnerabilities, please **do not open a public issue**. See
[SECURITY.md](SECURITY.md) for the disclosure process and self-hosting
guidance.

## License

[MIT](LICENSE) — see the LICENSE file for details.
