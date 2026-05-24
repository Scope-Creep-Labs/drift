#!/usr/bin/env bash
# Release Drift images to GHCR.
#
# Publishes:
#   ghcr.io/kidproquo/drift-agent:vYYYY.MM.DD-<sha>
#   ghcr.io/kidproquo/drift-frontend:vYYYY.MM.DD-<sha>     (VITE_BASE=/)
#
# Both also tagged :latest when released from `main`. On non-main
# branches the dated tag is the only one published — reference it
# explicitly by SHA.
#
# `drift-frontend` is built with VITE_BASE=/ so it serves at the
# domain root. That's the shape the single-server bundle in deploy/
# expects. If you need the /drift/ subpath build for the dabba.*
# deployment, build that one locally (see docker-compose.yml at the
# repo root, which has its own build: args).
#
# Requires:
#   - clean working tree (the tag encodes a specific HEAD)
#   - `docker login ghcr.io` already done with a PAT that has write:packages
#
# Usage:
#   scripts/release.sh           # builds + pushes both images
#   scripts/release.sh agent     # only drift-agent
#   scripts/release.sh frontend  # only drift-frontend

set -euo pipefail

REGISTRY="ghcr.io"
NS="kidproquo"
AGENT_IMAGE="$REGISTRY/$NS/drift-agent"
FRONTEND_IMAGE="$REGISTRY/$NS/drift-frontend"

cd "$(git rev-parse --show-toplevel)"

if ! git diff --quiet HEAD; then
  echo "ERROR: uncommitted changes. Commit or stash before releasing." >&2
  git status --short >&2
  exit 1
fi

DATE="$(date -u +%Y.%m.%d)"
SHA="$(git rev-parse --short HEAD)"
TAG="v${DATE}-${SHA}"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
WHICH=${1:-both}

build_agent() {
  echo "→ Building $AGENT_IMAGE:$TAG"
  docker build \
    -t "$AGENT_IMAGE:$TAG" \
    -f drift-agent/Dockerfile \
    .
  echo "→ Pushing $AGENT_IMAGE:$TAG"
  docker push "$AGENT_IMAGE:$TAG"
  if [ "$BRANCH" = "main" ]; then
    docker tag "$AGENT_IMAGE:$TAG" "$AGENT_IMAGE:latest"
    docker push "$AGENT_IMAGE:latest"
    echo "  → also pushed $AGENT_IMAGE:latest"
  fi
}

build_frontend() {
  echo "→ Building $FRONTEND_IMAGE:$TAG (VITE_BASE=/)"
  docker build \
    -t "$FRONTEND_IMAGE:$TAG" \
    --build-arg VITE_ENGINE=agent \
    --build-arg VITE_BASE=/ \
    -f Dockerfile \
    .
  echo "→ Pushing $FRONTEND_IMAGE:$TAG"
  docker push "$FRONTEND_IMAGE:$TAG"
  if [ "$BRANCH" = "main" ]; then
    docker tag "$FRONTEND_IMAGE:$TAG" "$FRONTEND_IMAGE:latest"
    docker push "$FRONTEND_IMAGE:latest"
    echo "  → also pushed $FRONTEND_IMAGE:latest"
  fi
}

case "$WHICH" in
  agent)    build_agent ;;
  frontend) build_frontend ;;
  both)     build_agent; build_frontend ;;
  *) echo "usage: $0 [agent|frontend|both]" >&2; exit 2 ;;
esac

cat <<EOF

✓ Released tag: $TAG  (branch: $BRANCH)

On a single-server bundle host (deploy/):
  cd deploy && docker compose pull && docker compose up -d
EOF
