# AlertTriage v2 — Production Dockerfile
#
# Multi-stage build:
#   Stage 1 (builder)    — installs all Python dependencies into /opt/venv
#   Stage 2 (production) — minimal runtime image, non-root user, no build tools
#
# Quick start:
#   docker build -t alerttriage .
#   docker run --env-file .env alerttriage --client-id acme-corp
#
# To run the API instead:
#   docker run --env-file .env -p 8000:8000 \
#     --entrypoint python alerttriage scripts/run_api.py

# ── Stage 1: Dependency builder ────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Create a venv so the production stage gets a clean, portable package set.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY requirements.txt requirements-api.txt ./

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir \
       -r requirements.txt \
       -r requirements-api.txt


# ── Stage 2: Production runtime ─────────────────────────────────────────────
FROM python:3.12-slim AS production

LABEL org.opencontainers.image.title="AlertTriage" \
      org.opencontainers.image.description="AI-powered security alert triage" \
      org.opencontainers.image.version="2.0.0"

# Copy the venv (owned by root but readable by all)
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Application lives under /app/alerttriage so that ``alerttriage.*`` imports
# resolve correctly (the parent /app is added to sys.path at runtime).
WORKDIR /app/alerttriage

# Create non-root user and persistent-data directories before COPY so that
# the chown is applied in a single layer.
RUN useradd --create-home --shell /bin/bash alerttriage \
    && mkdir -p /data /reports

COPY --chown=alerttriage:alerttriage . /app/alerttriage/

RUN chown -R alerttriage:alerttriage /data /reports

USER alerttriage

# Runtime environment defaults (override via --env-file or -e)
ENV ALERTTRIAGE_DATA_DIR=/data \
    ALERTTRIAGE_REPORTS_DIR=/reports \
    ALERTTRIAGE_LOG_LEVEL=INFO \
    ALERTTRIAGE_JSON_LOGS=true \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Expose API port
EXPOSE 8000

# Persistent storage — mount host volumes here in production
VOLUME ["/data", "/reports"]

# Health check hits the /health endpoint when the API is running.
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c \
    "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" \
    || exit 1

# Default: run the CLI triage runner.
# Pass --client-id <id> as the docker run argument, e.g.:
#   docker run alerttriage --client-id acme-corp
#
# To run the API server, override the entrypoint:
#   docker run --entrypoint python alerttriage scripts/run_api.py
ENTRYPOINT ["python", "scripts/run_client.py"]
