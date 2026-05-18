# Contributing to AlertTriage

Thank you for considering a contribution. This document covers how to report bugs, propose changes, and get your code merged.

## Table of contents

- [Code of conduct](#code-of-conduct)
- [Reporting bugs](#reporting-bugs)
- [Suggesting features](#suggesting-features)
- [Development setup](#development-setup)
- [Code style](#code-style)
- [Testing](#testing)
- [Pull request process](#pull-request-process)
- [Adding a new SIEM connector](#adding-a-new-siem-connector)
- [Adding a new AI model backend](#adding-a-new-ai-model-backend)

---

## Code of conduct

Be respectful. Harassment, discrimination, or hostile behaviour toward any contributor will not be tolerated.

---

## Reporting bugs

1. **Search existing issues** before opening a new one.
2. Open a GitHub issue with the **Bug report** template.
3. Include: Python version, OS, AlertTriage version (`alerttriage.__version__`), steps to reproduce, expected vs. actual behaviour, and any relevant logs (redact client data and API keys before pasting).

---

## Suggesting features

Open a GitHub issue with the **Feature request** template. Describe the use case, not just the solution — understanding *why* you need something helps us evaluate it better.

---

## Development setup

```bash
# 1. Fork and clone
git clone https://github.com/tbustenk/alerttriage.git
cd alerttriage

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# 3. Install in editable mode with dev extras
pip install -e ".[dev]"

# 4. Copy and configure environment
cp .env.example .env
# Set ANTHROPIC_API_KEY (and OPENAI_API_KEY if testing GPT backends)

# 5. Verify the test suite passes
pytest tests/ -v
```

---

## Code style

We use [Ruff](https://docs.astral.sh/ruff/) for both linting and formatting, and [Mypy](https://mypy-lang.org/) for static type checking. Configuration lives in `pyproject.toml`.

```bash
# Format
ruff format alerttriage/ scripts/ tests/

# Lint (with auto-fix)
ruff check --fix alerttriage/ scripts/ tests/

# Type check
mypy alerttriage/ --ignore-missing-imports
```

All three must pass cleanly before a PR can be merged. The CI workflow (`ci.yml`) runs them automatically.

**Key conventions:**

- Python 3.11+ syntax and type hints throughout (`from __future__ import annotations` at the top of every module).
- `async`/`await` for all I/O — no blocking calls in the hot path.
- Pydantic v2 for all data models.
- `structlog` for logging — structured key=value pairs, not f-strings in log calls.
- No bare `except:` — always catch a specific exception type.
- One class per file where practical; keep files under ~300 lines.

---

## Testing

```bash
# Run all tests
pytest tests/ -v

# Run a specific file
pytest tests/test_feedback.py -v

# Run with coverage
pytest tests/ --cov=alerttriage --cov-report=term-missing
```

**Test requirements:**

- Every new feature needs at least one unit test.
- Tests that touch `AlertAnalyzer` must use `dry_run=True` — the test suite must work without real API keys.
- New SIEM connectors need a test using a mocked `httpx` client.
- Do not commit tests that require live SIEM or AI credentials.

---

## Pull request process

1. **Branch naming:** `fix/short-description`, `feat/short-description`, `docs/short-description`.
2. **Keep PRs focused.** One logical change per PR — avoid unrelated cleanups mixed in with a feature.
3. **Update docs** if you change behaviour visible to users (README, `docs/`, docstrings).
4. **Pass CI** — all lint, type-check, and test jobs must be green before review.
5. **Changelog** — add a line under `## Unreleased` in `CHANGELOG.md` (if present).
6. At least one maintainer approval is required to merge.

### Commit message format

```
<type>(<scope>): <short summary>

[optional body]
```

Types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`.  
Scope (optional): `core`, `models`, `splunk`, `elk`, `feedback`, `ci`, etc.

Examples:
```
feat(models): add Gemini backend with fallback support
fix(anonymize): handle IPv6 addresses in PII scrubber
docs(api): add FeedbackSystem.accuracy() to API.md
```

---

## Adding a new SIEM connector

1. Create `alerttriage/src/integrations/<name>_connector.py`.
2. Subclass `SIEMConnector` (`siem_base.py`) and implement `fetch_alerts()`, `send_result()`, `health_check()`.
3. Register it in `scripts/run_client.py`'s `_build_connector()`.
4. Add a config example to `alerttriage/config/client_configs/template.yaml`.
5. Add a test in `tests/` using `pytest-httpx` or `unittest.mock` to mock HTTP calls.
6. Document it in `docs/ARCHITECTURE.md` under "How to add a new SIEM connector".

See `splunk_connector.py` and `elk_connector.py` as reference implementations.

---

## Adding a new AI model backend

1. Add the model spec to `alerttriage/config/models.yaml`.
2. If it uses a new provider, create `alerttriage/src/models/<provider>_backend.py` with an async `analyze(alert) -> AnalysisResult` method.
3. Register the provider in `ModelRouter._load_backends()`.
4. Document pricing in the backend file (`_PRICING` dict pattern).
5. Add `--ignore-missing-imports` stub note to `pyproject.toml` if the SDK lacks type stubs.

---

## Questions?

Open a GitHub Discussion or file an issue tagged `question`.
