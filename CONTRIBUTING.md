# Contributing to AlertTriage v2

Thank you for considering a contribution. This document covers the development setup, code standards, testing requirements, PR process, architecture decision records, and release process.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Reporting Bugs](#reporting-bugs)
- [Suggesting Features](#suggesting-features)
- [Development Setup](#development-setup)
- [Code Style](#code-style)
- [Running Tests](#running-tests)
- [PR Checklist](#pr-checklist)
- [Architecture Decisions](#architecture-decisions)
- [Adding a New SIEM Connector](#adding-a-new-siem-connector)
- [Adding a New AI Model Backend](#adding-a-new-ai-model-backend)
- [Release Process](#release-process)

---

## Code of Conduct

Be respectful. Harassment, discrimination, or hostile behaviour toward any contributor will not be tolerated.

---

## Reporting Bugs

1. **Search existing issues** before opening a new one.
2. Open a GitHub issue with the **Bug report** template.
3. Include: Python version, OS, AlertTriage version, steps to reproduce, expected vs. actual behaviour, and relevant logs. Redact all client data and API keys before pasting.

---

## Suggesting Features

Open a GitHub issue with the **Feature request** template. Describe the use case, not just the solution — understanding *why* you need something helps evaluate it better. If the feature changes the API or config schema, include a proposed interface.

---

## Development Setup

```bash
# 1. Fork and clone — the directory MUST be named "alerttriage"
git clone https://github.com/tbustenk/alerttriage.git alerttriage
cd alerttriage

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# 3. Install in editable mode with all extras
pip install -e .
pip install -r requirements-api.txt
pip install -r requirements-analytics.txt

# 4. Install dev tools
pip install pytest pytest-asyncio pytest-cov ruff mypy

# 5. Copy and configure the environment
cp .env.example .env
# At minimum, set ANTHROPIC_API_KEY for live tests
# Set ALERTTRIAGE_API_KEYS=dev-key for API tests

# 6. Verify everything works
pytest tests/ -v
ruff check alerttriage/ scripts/ tests/
mypy alerttriage/ --ignore-missing-imports
```

### Running the API Locally

```bash
python scripts/run_api.py --host 127.0.0.1 --port 8000
```

Interactive docs: `http://127.0.0.1:8000/docs`

### Running the Dashboard Locally

```bash
cd dashboard && pip install -r requirements.txt && python app.py
```

Dashboard: `http://localhost:5000`

---

## Code Style

AlertTriage uses [Ruff](https://docs.astral.sh/ruff/) for linting and formatting, and [Mypy](https://mypy-lang.org/) for static type checking. Configuration lives in `pyproject.toml`.

### Running the Checks

```bash
# Format code
ruff format alerttriage/ scripts/ tests/

# Lint with auto-fix (safe fixes only)
ruff check --fix alerttriage/ scripts/ tests/

# Type check
mypy alerttriage/ --ignore-missing-imports
```

All three must pass cleanly before a PR can be merged. CI runs them automatically on every push.

### Key Conventions

- **Python 3.12 syntax** with `from __future__ import annotations` at the top of every module.
- **`async`/`await` for all I/O** — no blocking calls (`time.sleep`, `requests.get`, `sqlite3.connect` with long timeouts) in the hot path.
- **Pydantic v2** for all data models. Use `BaseModel`, not dataclasses, for anything that crosses a module boundary.
- **`structlog` for logging** — emit structured key=value pairs, not f-strings in log calls:
  ```python
  # Correct:
  log.info("analysis_complete", alert_id=result.alert_id, verdict=result.verdict.value)

  # Wrong:
  log.info(f"Analysis complete for {result.alert_id}: {result.verdict}")
  ```
- **No bare `except:`** — always catch a specific exception type. Use `except Exception as exc:` with a comment (`# noqa: BLE001`) only in the batch runner where a single alert must not poison the gather.
- **One public class per file** where practical. Keep files under ~300 lines.
- **Side-effect imports** — if an import is needed for its side effect, protect it from `ruff`'s unused-import removal:
  ```python
  import alerttriage.src.monitoring  # noqa: F401
  ```

---

## Running Tests

```bash
# Run all tests
pytest tests/ -v

# Run a specific file
pytest tests/test_feedback.py -v

# Run a specific test
pytest tests/test_feedback.py::test_accuracy_empty_db -v

# Run with coverage report
pytest tests/ --cov=alerttriage --cov-report=term-missing

# Run only fast unit tests (skip slow integration tests)
pytest tests/ -m "not slow" -v
```

### Test Requirements

- **Every new feature needs at least one unit test.**
- **Tests that touch `AlertAnalyzer` must use `dry_run=True`** — the test suite must work without real API keys.
- **New SIEM connectors** need tests using a mocked `httpx` client (`pytest-httpx` or `unittest.mock`).
- **New API endpoints** need tests using `fastapi.testclient.TestClient`.
- **Do not commit tests** that require live SIEM credentials, live AI API calls, or network access.
- **Test coverage target:** 80% for new code. Check with `--cov-fail-under=80`.

### Test Data and Fixtures

Shared fixtures are in `conftest.py` at the repo root. Common fixtures:

| Fixture | Type | Description |
|---------|------|-------------|
| `tmp_data_dir` | `Path` | Temporary directory for SQLite databases |
| `sample_alert` | `Alert` | A minimal valid `Alert` object |
| `dry_run_result` | `AnalysisResult` | A placeholder result for testing downstream code |

---

## PR Checklist

Before submitting a pull request:

- [ ] Tests pass locally: `pytest tests/ -v`
- [ ] Ruff passes: `ruff check alerttriage/ scripts/ tests/`
- [ ] Mypy passes: `mypy alerttriage/ --ignore-missing-imports`
- [ ] New public functions and classes have docstrings
- [ ] If the API surface changed: `docs/API.md` is updated
- [ ] If config schema changed: `config/default_config.yaml` and `docs/ARCHITECTURE.md` are updated
- [ ] If a new dependency was added: `requirements.txt` or `requirements-api.txt` is updated
- [ ] If behaviour changed for operators: `docs/DEPLOYMENT.md` or `docs/TROUBLESHOOTING.md` is updated

### Branch Naming

| Type | Pattern | Example |
|------|---------|---------|
| Feature | `feat/<description>` | `feat/gemini-backend` |
| Bug fix | `fix/<description>` | `fix/anonymize-ipv6` |
| Documentation | `docs/<description>` | `docs/webhook-examples` |
| Refactor | `refactor/<description>` | `refactor/learning-engine` |
| Test | `test/<description>` | `test/rate-limiter` |
| Chore | `chore/<description>` | `chore/bump-anthropic-sdk` |

### Commit Message Format

```
<type>(<scope>): <short summary in imperative mood>

[optional longer body explaining why, not what]

[optional: Closes #123]
```

Types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `perf`.  
Scope: `core`, `api`, `models`, `feedback`, `analytics`, `webhooks`, `config`, `monitoring`, `splunk`, `elk`, `ci`.

**Examples:**

```
feat(models): add Gemini backend with fallback support
fix(anonymize): handle IPv6 addresses in PII scrubber
perf(feedback): add composite index on (client_id, rule_name)
docs(api): document context endpoint context_type values
```

### Review Process

- At least one maintainer approval is required to merge.
- CI must be green (lint, type-check, tests).
- PRs that change the API or config schema require a maintainer sign-off on the design before implementation review.

---

## Architecture Decisions

Key design choices and the rationale behind them:

### SQLite over PostgreSQL

The feedback store is SQLite per client. This eliminates a deployment dependency (no database server to operate), keeps client data isolated (one file per client), and makes backup trivial (copy one file). SQLite with WAL mode handles the read/write patterns of the feedback system comfortably up to millions of rows per client. A PostgreSQL migration path is supported via the `postgres` Docker Compose profile.

### No Message Broker

AlertTriage processes alerts synchronously through an async Python event loop. A message broker (Kafka, RabbitMQ, SQS) would add operational complexity without improving correctness. The `analyze_batch()` method with bounded concurrency provides adequate throughput for SOC workloads (up to ~100,000 alerts/day on a single instance).

### In-Memory Rate Limiting

The rate limiter is a per-process sliding window deque. No Redis or external state. This is acceptable because AlertTriage is typically deployed as a single instance, and rate limiting is a best-effort safety measure rather than a hard SLA. If precise cross-replica limiting is needed, put it at the load balancer.

### Prompt Enhancement Over Fine-Tuning

The learning engine generates natural-language hints injected into the system prompt rather than fine-tuning the model. This is simpler (no training infrastructure), works with any backend (Claude, GPT), and is fully reversible (delete feedback data to reset). Fine-tuning is a future option for very high-volume deployments.

### PII Anonymisation Approach

PII is replaced with stable pseudonyms (salted hash) rather than removed. This preserves cross-alert correlation (the same IP always produces the same pseudonym within a client) while preventing the actual values from reaching the AI backend. The reverse map lives only in RAM during a single analysis call.

---

## Adding a New SIEM Connector

1. Create `alerttriage/src/integrations/<name>_connector.py`.
2. Subclass `SIEMConnector` from `siem_base.py` and implement:
   - `async fetch_alerts(*, limit, since_id) -> list[Alert]`
   - `async send_result(result: AnalysisResult) -> bool`
   - `async health_check() -> bool`
   - (Optional) `async close()` to release HTTP sessions
3. Register it in `scripts/run_client.py`'s `_build_connector()` factory.
4. Add a config example in a comment at the top of the connector file and in `config/client_configs/template.yaml`.
5. Write tests in `tests/test_<name>_connector.py` using `pytest-httpx` to mock HTTP responses.
6. Update `docs/ARCHITECTURE.md` with a note about the new connector.

See `splunk_connector.py` and `elk_connector.py` as reference implementations.

---

## Adding a New AI Model Backend

1. Add a model spec to `config/models.yaml`:
   ```yaml
   gemini-flash:
     provider: google
     model_name: gemini-2.0-flash
     max_tokens: 2048
     temperature: 0.2
   ```
2. If it uses a new provider, create `alerttriage/src/models/<provider>_backend.py`:
   - Implement `async def analyze(alert: Alert, *, system_prompt: str, retry_config: RetryConfig) -> AnalysisResult`
   - Add a `_PRICING` dict with `{"input_per_1k": ..., "output_per_1k": ...}` for cost tracking
3. Register the provider in `ModelRouter._load_backends()`.
4. Add a `[[tool.mypy.overrides]]` entry in `pyproject.toml` if the SDK lacks type stubs.
5. Write tests using `unittest.mock` to patch the provider SDK.
6. Document any required environment variables in `docs/DEPLOYMENT.md`.

---

## Release Process

1. **Bump version** in `src/api/app.py`: `VERSION = "2.1.0"` and in `Dockerfile` LABEL.
2. **Update CHANGELOG.md** — move items from `## Unreleased` to the new version section.
3. **Run the full test suite** and confirm clean lint/type-check.
4. **Tag the release:**
   ```bash
   git tag -a v2.1.0 -m "AlertTriage v2.1.0"
   git push origin v2.1.0
   ```
5. **Build and push the Docker image:**
   ```bash
   docker build -t alerttriage:2.1.0 -t alerttriage:latest --target production .
   docker push alerttriage:2.1.0
   docker push alerttriage:latest
   ```
6. **Create a GitHub release** from the tag, copying the CHANGELOG section into the release notes.

---

## Questions?

Open a [GitHub Discussion](https://github.com/tbustenk/alerttriage/discussions) or file an issue tagged `question`.
