# AlertTriage v2

[![CI](https://github.com/tbustenk/alerttriage/actions/workflows/ci.yml/badge.svg)](https://github.com/tbustenk/alerttriage/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Production-grade AI alert analysis platform. Connects to your SIEM, anonymizes alerts, routes them through a configurable AI model, and writes verdicts back — with a feedback loop that learns from analyst decisions over time.

---

## Features

| Feature | Details |
|---|---|
| Multi-model AI | Claude (Sonnet / Opus / Haiku) and GPT (4o / 4o-mini); per-client selection with automatic fallback |
| SIEM connectors | Splunk Enterprise Security, Elasticsearch/ELK, generic HTTPS webhook |
| PII anonymization | IPs, usernames, card numbers, SSNs, and API keys stripped before any data reaches an AI provider |
| Feedback loop | Analyst verdicts stored in SQLite; `LearningEngine` generates prompt hints to improve future accuracy |
| Cost control | Per-client daily and monthly hard limits; spend tracked across restarts |
| Reporting | HTML and JSON reports with accuracy, cost, and verdict breakdown metrics |
| Multi-tenant | Isolated config, data, and cost state per client — no cross-client data leakage |
| Prompt caching | Claude backend uses Anthropic's prompt caching; repeated system prompts billed at ~10% of normal input rate |

---

## Quick start

### Prerequisites

- Python 3.11 or newer
- An [Anthropic API key](https://console.anthropic.com/) (and/or OpenAI key for GPT backends)
- Access credentials for your SIEM (Splunk token or ELK API key)

### Install

```bash
git clone https://github.com/tbustenk/alerttriage.git
cd alerttriage
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e .
```

### Configure

```bash
cp .env.example .env
# Edit .env — add ANTHROPIC_API_KEY (and OPENAI_API_KEY if using GPT)
```

### Onboard a client (interactive wizard)

```bash
python scripts/init_client.py
```

The wizard asks for the client name, SIEM type, model preference, and cost limits.
It writes `alerttriage/config/client_configs/<client_id>.yaml` and creates `data/<client_id>/`.

### Verify connectivity (dry run — no AI calls, no SIEM writes)

```bash
python scripts/run_client.py --client-id <id> --limit 10 --dry-run
```

### Run a full triage batch

```bash
python scripts/run_client.py --client-id <id> --limit 100
```

---

## Project layout

```
alerttriage/                    # git repository root
├── alerttriage/                # Python package
│   ├── config/
│   │   ├── config_manager.py   # Loads and merges YAML + env vars
│   │   ├── default_config.yaml # System-wide defaults
│   │   ├── models.yaml         # AI model registry
│   │   └── client_configs/     # One YAML per client (template included)
│   └── src/
│       ├── core/
│       │   ├── analyzer.py     # Orchestrator — main entry point
│       │   ├── alert_models.py # Alert, FeedbackRecord (Pydantic)
│       │   └── result_models.py# AnalysisResult, Verdict (Pydantic)
│       ├── models/
│       │   ├── model_router.py # Backend selection + fallback chain
│       │   ├── claude_backend.py
│       │   └── gpt_backend.py
│       ├── integrations/
│       │   ├── siem_base.py    # Abstract base — implement to add a SIEM
│       │   ├── splunk_connector.py
│       │   ├── elk_connector.py
│       │   └── webhook_output.py
│       ├── feedback/
│       │   ├── feedback_system.py  # SQLite store for analyst decisions
│       │   └── learning_engine.py  # Derives prompt hints from feedback
│       ├── reporting/
│       │   ├── metrics.py          # Accuracy, cost, verdict calculations
│       │   └── report_generator.py # HTML + JSON report output
│       ├── anonymize.py        # PII pseudonymisation (runs before every AI call)
│       ├── cost_controller.py  # Per-client budget enforcement
│       └── logger.py           # Structured logging (structlog)
├── scripts/
│   ├── init_client.py          # Interactive new-client wizard
│   ├── run_client.py           # Main triage run loop
│   └── migrate_config.py       # Config migration between versions
├── tests/                      # pytest unit tests (no live API keys required)
├── docs/
│   ├── ARCHITECTURE.md         # System design and data flow
│   ├── CLIENT_SETUP.md         # Onboarding and operational guide
│   └── API.md                  # Class and method reference
├── .github/workflows/ci.yml    # Lint, type-check, test, security scan
├── pyproject.toml              # Ruff, Mypy, and Pytest configuration
├── requirements.txt            # Runtime dependencies
├── setup.py                    # Package metadata and dev extras
├── .env.example                # Environment variable template
├── CONTRIBUTING.md
├── SECURITY.md
└── LICENSE
```

---

## Documentation

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How alerts flow through the system, how the feedback loop works, how to extend the platform |
| [docs/CLIENT_SETUP.md](docs/CLIENT_SETUP.md) | Onboarding walkthrough, config reference, credential rotation, cost management |
| [docs/API.md](docs/API.md) | Class and method reference for `AlertAnalyzer`, `Alert`, `AnalysisResult`, connectors, and more |

---

## Running tests

```bash
# Install dev extras first
pip install -e ".[dev]"

# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=alerttriage --cov-report=term-missing
```

Tests run in dry-run / offline mode — no live API keys or SIEM access required.

---

## Adding a new AI model

1. Add a spec to `alerttriage/config/models.yaml` (`provider`, `model_name`, `max_tokens`, `temperature`).
2. If it's a new provider, create `alerttriage/src/models/<provider>_backend.py` implementing `async def analyze(alert) -> AnalysisResult`.
3. Register it in `ModelRouter._load_backends()`.
4. Set it in a client config: `model: <new-model-id>`.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full guide.

---

## Adding a new SIEM connector

1. Create `alerttriage/src/integrations/<name>_connector.py`, subclassing `SIEMConnector`.
2. Implement `fetch_alerts()`, `send_result()`, and `health_check()`.
3. Register it in `scripts/run_client.py`'s `_build_connector()` factory.
4. Add a config example to `alerttriage/config/client_configs/template.yaml`.

---

## Contributing

We welcome bug reports, feature requests, and pull requests. Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a PR.

---

## Security

For security vulnerabilities, please **do not open a public issue**. See [SECURITY.md](SECURITY.md) for the responsible disclosure process and self-hosting security guidance.

---

## License

[MIT](LICENSE) — see the LICENSE file for details.
