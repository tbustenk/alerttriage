#!/usr/bin/env bash
# Build the AlertTriage Docker image.
#
# Usage:
#   ./scripts/docker_build.sh                   # build with tag alerttriage:latest
#   ./scripts/docker_build.sh --tag v2.1.0      # additional tag
#   ./scripts/docker_build.sh --no-cache        # force full rebuild
#   ./scripts/docker_build.sh --push            # push to registry after build

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_NAME="${ALERTTRIAGE_IMAGE:-alerttriage}"
TAG="${ALERTTRIAGE_TAG:-latest}"
PUSH=false
EXTRA_ARGS=()

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --tag)       TAG="$2"; shift 2 ;;
        --image)     IMAGE_NAME="$2"; shift 2 ;;
        --push)      PUSH=true; shift ;;
        --no-cache)  EXTRA_ARGS+=("--no-cache"); shift ;;
        --platform)  EXTRA_ARGS+=("--platform" "$2"); shift 2 ;;
        *)           echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Building AlertTriage Docker image"
echo "  Image:   ${IMAGE_NAME}:${TAG}"
echo "  Context: ${REPO_ROOT}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

docker build \
    --file "${REPO_ROOT}/Dockerfile" \
    --tag "${IMAGE_NAME}:${TAG}" \
    --tag "${IMAGE_NAME}:latest" \
    "${EXTRA_ARGS[@]}" \
    "${REPO_ROOT}"

echo ""
echo "✓ Build complete: ${IMAGE_NAME}:${TAG}"

if [[ "${PUSH}" == "true" ]]; then
    echo "Pushing ${IMAGE_NAME}:${TAG} ..."
    docker push "${IMAGE_NAME}:${TAG}"
    docker push "${IMAGE_NAME}:latest"
    echo "✓ Push complete"
fi

echo ""
echo "Run the API:   docker run --env-file .env -p 8000:8000 --entrypoint python ${IMAGE_NAME}:${TAG} scripts/run_api.py"
echo "Run triage:    docker run --env-file .env ${IMAGE_NAME}:${TAG} --client-id <id>"
