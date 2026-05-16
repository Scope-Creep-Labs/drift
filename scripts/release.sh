#!/usr/bin/env bash
# Release drift-agent to GHCR.
#
# Tag scheme: vYYYY.MM.DD-<short-sha>  (always emitted)
#                :latest               (only when releasing from main)
#
# Requires:
#   - clean working tree (the tag encodes a specific HEAD)
#   - `docker login ghcr.io` already done with a PAT that has write:packages
#
# Usage:
#   scripts/release.sh

set -euo pipefail

REGISTRY="ghcr.io"
IMAGE="$REGISTRY/kidproquo/drift-agent"

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

echo "→ Building $IMAGE:$TAG"
docker build \
  -t "$IMAGE:$TAG" \
  -f drift-agent/Dockerfile \
  .

echo "→ Pushing $IMAGE:$TAG"
docker push "$IMAGE:$TAG"

if [ "$BRANCH" = "main" ]; then
  echo "→ Tagging :latest (on main)"
  docker tag "$IMAGE:$TAG" "$IMAGE:latest"
  docker push "$IMAGE:latest"
else
  echo "→ Skipping :latest (not on main; branch=$BRANCH)"
  echo "  Reference this build by its dated tag: $IMAGE:$TAG"
fi

cat <<EOF

✓ Released $IMAGE:$TAG

On each drift-agent host:
  docker compose pull drift-agent
  docker compose up -d drift-agent

The new in-image drift-deploy-agent.sh propagates to managed devices on
the next check-in (~30s per device).
EOF
