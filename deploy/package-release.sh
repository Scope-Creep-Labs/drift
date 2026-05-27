#!/usr/bin/env bash
# Package the Drift single-server installer into a release tarball
# suitable for attaching to a GitHub release.
#
# Bundles only the source-of-truth files (everything `git ls-files`
# reports under deploy/). Skips: .env, logs/, rendered templates
# (*.sample, config/auth.yml, config/grafana.ini, etc.), runtime
# state directories.
#
# Usage:
#   ./package-release.sh                                    # version from git describe
#   ./package-release.sh v0.3.1                             # explicit version tag
#   ./package-release.sh --allow-dirty                      # include uncommitted changes
#   ./package-release.sh --publish v0.3.1                   # also `gh release create`
#   ./package-release.sh --publish v0.3.1 --repo me/pubrel  # publish to a different repo
#
# --repo is for the "public releases repo for a private source repo"
# pattern: source lives in owner/drift (private), releases ship from
# owner/drift-releases (public). The tag still has to exist in the
# target repo, so gh will create it from a default branch ref unless
# you pass --target.
#
# Output goes to deploy/dist/ (gitignored).

set -euo pipefail

cd "$(dirname "$0")"
DEPLOY_DIR=$(pwd)
REPO_ROOT=$(git rev-parse --show-toplevel)

# ---------- args ----------
VERSION=""
ALLOW_DIRTY=false
PUBLISH=false
IMAGE_ONLY=false
NOTES_FILE=""
TARGET_REPO=""
TARGET_BRANCH=""

while [ $# -gt 0 ]; do
  case "$1" in
    --allow-dirty) ALLOW_DIRTY=true; shift ;;
    --image-only)  IMAGE_ONLY=true; PUBLISH=true; shift ;;
    --publish)     PUBLISH=true; shift ;;
    --notes)       NOTES_FILE=$2; shift 2 ;;
    --repo)        TARGET_REPO=$2; shift 2 ;;
    --target)      TARGET_BRANCH=$2; shift 2 ;;
    -h|--help)
      sed -n '2,25p' "$0"; exit 0 ;;
    -*)
      echo "unknown flag: $1" >&2; exit 1 ;;
    *)
      VERSION=$1; shift ;;
  esac
done

# ---------- version ----------
if [ -z "$VERSION" ]; then
  # Prefer a tag if HEAD has one; else `git describe` gives e.g. v0.3.0-5-gabc1234.
  if VERSION=$(git -C "$REPO_ROOT" describe --tags --exact-match HEAD 2>/dev/null); then
    :
  elif VERSION=$(git -C "$REPO_ROOT" describe --tags --always --dirty 2>/dev/null); then
    :
  else
    VERSION="0.0.0-$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
  fi
fi
# Strip a leading "v" from the version when used in filenames (matches GH convention).
VERSION_FILE="${VERSION#v}"

echo "→ packaging drift-deploy version: $VERSION"

# ---------- dirty-tree check ----------
DIRTY_FILES=$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null || true)
if [ -n "$DIRTY_FILES" ]; then
  if [ "$ALLOW_DIRTY" = "false" ]; then
    echo "  ✗ working tree has uncommitted changes:" >&2
    echo "$DIRTY_FILES" | sed 's/^/      /' >&2
    echo >&2
    echo "  Commit them first, or re-run with --allow-dirty." >&2
    echo "  Why this matters: package-release.sh's smart-build diffs HEAD" >&2
    echo "  against the previous release tag to decide which images need" >&2
    echo "  rebuilding. Uncommitted changes in drift-agent/ or src/ would" >&2
    echo "  ship into the tarball but be invisible to the diff → images" >&2
    echo "  stale, tarball ahead." >&2
    exit 1
  fi
  echo "  ⚠ working tree is dirty — packaging anyway (--allow-dirty)"
fi

# ---------- output dir ----------
DIST_DIR="$DEPLOY_DIR/dist"
mkdir -p "$DIST_DIR"
PREFIX="drift-deploy-$VERSION_FILE"
TARBALL="$DIST_DIR/${PREFIX}.tar.gz"
SHA_FILE="${TARBALL}.sha256"

# ---------- build tarball ----------
# Clean-tree path: `git archive` picks up exactly the committed files
# under deploy/, with the right modes (install.sh stays +x because it's
# tracked as +x in git's index).
#
# Dirty-tree path: enumerate via `git ls-files` (still excludes ignored
# + untracked junk), copy into a staging dir, tar from there. This
# preserves modes from the working tree, which is what --allow-dirty
# implies.
# Files we explicitly don't want in the end-user tarball even though
# they're tracked under deploy/ (maintainer tools, etc.).
EXCLUDE_FROM_RELEASE=(
  "package-release.sh"
)

# Build a list of tracked deploy/ files, minus the exclusions.
collect_files() {
  ( cd "$REPO_ROOT" && git ls-files deploy/ ) | while IFS= read -r f; do
    rel="${f#deploy/}"
    local skip=false
    for ex in "${EXCLUDE_FROM_RELEASE[@]}"; do
      [ "$rel" = "$ex" ] && { skip=true; break; }
    done
    [ "$skip" = "true" ] || echo "$f"
  done
}

if [ "$IMAGE_ONLY" = "true" ]; then
  echo "  ⓘ --image-only: skipping tarball build (release will have no bundle asset)"
else
  WORK=$(mktemp -d)
  STAGE="$WORK/$PREFIX"
  mkdir -p "$STAGE"

  if [ "$ALLOW_DIRTY" = "false" ]; then
    # Clean tree: pull each file from HEAD via `git show` so we get the
    # committed contents (not the working tree).
    collect_files | while IFS= read -r f; do
      rel="${f#deploy/}"
      mkdir -p "$STAGE/$(dirname "$rel")"
      git -C "$REPO_ROOT" show "HEAD:$f" > "$STAGE/$rel"
    done
  else
    # Dirty tree: copy from the working tree.
    collect_files | while IFS= read -r f; do
      rel="${f#deploy/}"
      mkdir -p "$STAGE/$(dirname "$rel")"
      cp -p "$REPO_ROOT/$f" "$STAGE/$rel"
    done
  fi

  # Preserve +x on the installer (git stores mode bits but `git show`
  # strips them on stdout, so re-apply explicitly).
  chmod +x "$STAGE/install.sh"

  # Stamp the release tag into install.sh so the running stack can
  # report its bundle version to the admin update modal. Source's
  # placeholder is `INSTALL_VERSION="dev"`; we sed it to the real tag.
  sed -i.bak "s|^INSTALL_VERSION=\"dev\"$|INSTALL_VERSION=\"$VERSION\"|" "$STAGE/install.sh" \
    && rm -f "$STAGE/install.sh.bak"

  tar -czf "$TARBALL" -C "$WORK" "$PREFIX"
  rm -rf "$WORK"

  # ---------- checksum ----------
  ( cd "$DIST_DIR" && sha256sum "$(basename "$TARBALL")" > "$(basename "$SHA_FILE")" )

  # ---------- summary ----------
  SIZE=$(du -h "$TARBALL" | awk '{print $1}')
  SHA=$(awk '{print $1}' "$SHA_FILE")
  echo
  echo "  ✓ wrote $TARBALL  ($SIZE)"
  echo "  ✓ wrote $SHA_FILE"
  echo "      sha256: $SHA"
  echo
  echo "  Inspect contents:"
  echo "      tar -tzf $TARBALL | head"
fi
echo

# ---------- (optional) publish to GitHub ----------
if [ "$PUBLISH" = "true" ]; then
  if ! command -v gh >/dev/null; then
    echo "  ✗ --publish set but \`gh\` CLI not found" >&2
    exit 1
  fi
  if [ "$ALLOW_DIRTY" = "true" ]; then
    echo "  ✗ refusing to --publish a dirty build (rerun without --allow-dirty)" >&2
    exit 1
  fi
  # Use the version tag verbatim; if user passed a non-tag version like
  # 0.0.0-abc1234, this'll create the tag too.
  REL_TAG="$VERSION"

  # ---------- build + push the images for this release ----------
  # Only rebuild + push images whose source changed since the previous
  # release tag. Otherwise we'd stamp a fresh VERSION label on every
  # image every release — producing a new digest with no real code
  # change, which surfaces in the modal as a spurious "update
  # available" for the unchanged service. The git-diff check below
  # makes the image graph track actual code, not release cadence.
  #
  # Path mapping for each service — any change under these paths
  # qualifies as a real source change:
  #   drift-agent:    drift-agent/  +  edge-agent/  (the latter is
  #                   COPY'd into the image at build time, so it's
  #                   functionally part of drift-agent's sources)
  #   drift-frontend: src/  +  index.html  +  Dockerfile  +
  #                   package.json / package-lock.json  +
  #                   vite.config.ts / tsconfig.json
  #
  # The previous-release tag is whatever `git describe --tags
  # --abbrev=0` reports. First-ever release (no prior tag) → always
  # build both.
  LAST_TAG=$(git -C "$REPO_ROOT" describe --tags --abbrev=0 2>/dev/null || echo "")
  echo "→ building + pushing images that changed since ${LAST_TAG:-(no prior tag)}"

  paths_drift_agent="drift-agent edge-agent"
  paths_drift_frontend="src index.html Dockerfile package.json package-lock.json vite.config.ts tsconfig.json"

  build_image() {
    local name=$1 dockerfile=$2 paths=$3
    local image="ghcr.io/kidproquo/$name"
    local changed="yes"
    if [ -n "$LAST_TAG" ]; then
      # shellcheck disable=SC2086
      if [ -z "$(git -C "$REPO_ROOT" diff --name-only "$LAST_TAG" HEAD -- $paths 2>/dev/null | head -1)" ]; then
        changed="no"
      fi
    fi
    if [ "$changed" = "no" ]; then
      echo "   $image: unchanged since $LAST_TAG, skipping"
      return
    fi
    local df_arg=()
    [ -n "$dockerfile" ] && df_arg=(-f "$dockerfile")
    echo "   $image:$REL_TAG"
    docker build "${df_arg[@]}" \
      --build-arg "VERSION=$REL_TAG" \
      -t "$image:$REL_TAG" \
      -t "$image:latest" \
      "$REPO_ROOT" >/dev/null
    docker push "$image:$REL_TAG"  >/dev/null
    docker push "$image:latest"    >/dev/null
  }

  build_image drift-agent    "$REPO_ROOT/drift-agent/Dockerfile" "$paths_drift_agent"
  build_image drift-frontend ""                                  "$paths_drift_frontend"
  echo "  ✓ image build/push complete"

  repo_arg=()
  if [ -n "$TARGET_REPO" ]; then
    echo "→ creating GitHub release $REL_TAG in $TARGET_REPO"
    repo_arg=(--repo "$TARGET_REPO")
  else
    echo "→ creating GitHub release $REL_TAG (current repo)"
  fi
  target_arg=()
  if [ -n "$TARGET_BRANCH" ]; then
    target_arg=(--target "$TARGET_BRANCH")
  fi
  notes_arg=()
  if [ -n "$NOTES_FILE" ]; then
    notes_arg=(--notes-file "$NOTES_FILE")
  else
    notes_arg=(--generate-notes)
  fi
  # Cross-repo gotcha: if --repo points at a repo other than this
  # working tree's, the source tag may not exist there. gh creates it
  # on the target's default branch unless --target is set. --generate-notes
  # also needs the tag history in the target repo to be useful; if you're
  # publishing to an empty releases repo, prefer --notes <file>.
  if [ -n "$TARGET_REPO" ] && [ -z "$NOTES_FILE" ]; then
    echo "  ⚠ --generate-notes against a fresh --repo can be empty; consider --notes <file>"
  fi
  # --image-only: don't attach the tarball + sha. The admin updates
  # poller detects has_bundle_changes from "is there a drift-deploy-
  # *.tar.gz asset?" so omitting them flags this release as image-only
  # → no bundle banner in the modal, no re-install nag for the user.
  assets_arg=()
  if [ "$IMAGE_ONLY" = "false" ]; then
    assets_arg=("$TARBALL" "$SHA_FILE")
  else
    echo "  ⓘ --image-only: skipping tarball asset upload"
  fi
  gh release create "$REL_TAG" \
    "${assets_arg[@]}" \
    --title "Drift deploy $REL_TAG" \
    "${repo_arg[@]}" \
    "${target_arg[@]}" \
    "${notes_arg[@]}"
  echo "  ✓ release published"

  # Tag the source repo at HEAD so the NEXT release's smart-build
  # diff has a previous tag to compare against. Without this, every
  # build_image() call defaults to "yes, rebuild" since LAST_TAG is
  # empty. Idempotent: skip if the tag already exists locally.
  if ! git -C "$REPO_ROOT" rev-parse "$REL_TAG" >/dev/null 2>&1; then
    git -C "$REPO_ROOT" tag "$REL_TAG"
    echo "  ✓ tagged source repo @ HEAD = $REL_TAG (push with: git push origin $REL_TAG)"
  fi
fi
