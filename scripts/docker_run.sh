#!/usr/bin/env bash
# Run AlertTriage in Docker.
#
# Usage:
#   ./scripts/docker_run.sh --client-id acme-corp          # run triage
#   ./scripts/docker_run.sh --api                          # start API server
#   ./scripts/docker_run.sh --api --port 8080              # API on custom port
#   ./scripts/docker_run.sh --client-id acme-corp --dry-run

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_NAME="${ALERTTRIAGE_IMAGE:-alerttriage}"
TAG="${ALERTTRIAGE_TAG:-latest}"

MODE="triage"        # triage | api
CLIENT_ID=""
API_PORT=8000
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --api)        MODE="api"; shift ;;
        --client-id)  CLIENT_ID="$2"; shift 2 ;;
        --port)       API_PORT="$2"; shift 2 ;;
        --dry-run)    EXTRA_ARGS+=("--dry-run"); shift ;;
        --limit)      EXTRA_ARGS+=("--limit" "$2"); shift 2 ;;
        --image)      IMAGE_NAME="$2"; shift 2 ;;
        --tag)        TAG="$2"; shift 2 ;;
        *)            echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

# Ensure .env exists
ENV_FILE="${REPO_ROOT}/.env"
if [[ ! -f "${ENV_FILE}" ]]; then
    echo "Warning: .env not found at ${ENV_FILE}"
    echo "Copy .env.example to .env and fill in your secrets first."
    echo "Continuing without .env ..."
    ENV_FILE="/dev/null"
fi

DOCKER_ARGS=(
    "--rm"
    "--env-file" "${ENV_FILE}"
    "--volume" "alerttriage-data:/data"
    "--volume" "alerttriage-reports:/reports"
    "--volume" "${REPO_ROOT}/config:/app/alerttriage/config:ro"
)

if [[ "${MODE}" == "api" ]]; then
    echo "Starting AlertTriage API on port ${API_PORT} ..."
    docker run \
        "${DOCKER_ARGS[@]}" \
        --publish "${API_PORT}:8000" \
        --name alerttriage-api \
        --detach \
        --restart unless-stopped \
        --entrypoint python \
        "${IMAGE_NAME}:${TAG}" \
        scripts/run_api.py --host 0.0.0.0 --port 8000

    echo "✓ API started — http://localhost:${API_PORT}"
    echo "  Swagger docs:  http://localhost:${API_PORT}/docs"
    echo "  Health check:  http://localhost:${API_PORT}/health"
    echo "  Metrics:       http://localhost:${API_PORT}/metrics"
    echo ""
    echo "Stop with: docker stop alerttriage-api"

elif [[ "${MODE}" == "triage" ]]; then
    if [[ -z "${CLIENT_ID}" ]]; then
        echo "Error: --client-id is required for triage mode" >&2
        echo "Usage: $0 --client-id <id>" >&2
        exit 1
    fi

    echo "Running triage for client: ${CLIENT_ID}"
    docker run \
        "${DOCKER_ARGS[@]}" \
        "${IMAGE_NAME}:${TAG}" \
        --client-id "${CLIENT_ID}" \
        "${EXTRA_ARGS[@]}"
fi
