# Drift updates — model and workflow

> Installer + day-2 ops live in [deploy/README.md](./README.md); project overview at the top-level [README.md](../README.md). This doc is the contract between release authors and operators.

Drift's CP (`drift-agent` + `drift-frontend`) ships as docker images, while the surrounding bundle (`install.sh`, `docker-compose.yml`, `config/*.tmpl`) is a tarball you re-extract on the host. The two move on different cadences, so update *handling* is split into two paths. This doc describes the model so release authors and operators stay aligned.

---

## Two kinds of release

Every release tagged in [Scope-Creep-Labs/drift](https://github.com/Scope-Creep-Labs/drift/releases) is one of two kinds. They look the same in `gh release list`; the differentiator is whether a tarball asset is attached.

### Image-only release

* **Content**: just Python (`drift-agent`) and/or SPA (`drift-frontend`) code changes.
* **Trigger**: `package-release.sh --publish vX.Y.Z --image-only --notes notes.md`
* **What the script does**: build both images with `--build-arg VERSION=vX.Y.Z`, tag them as `:vX.Y.Z` AND `:latest`, push both tags, create the GitHub release **without** a tarball asset.
* **Operator action**: open the Software updates modal → click **Update now**. Images pull, containers recreate, done.
* **Modal indicator**: small `image-only` chip on the release entry.

### Bundle release

* **Content**: anything that touches `install.sh`, `docker-compose.yml`, `config/*.tmpl`, or any file in the deploy bundle. Usually paired with code changes too.
* **Trigger**: `package-release.sh --publish vX.Y.Z --notes notes.md`
* **What the script does**: everything above **plus** build the tarball, sha256 it, attach both to the GitHub release.
* **Operator action**: **manual upgrade required**. Run the curl + tar + `install.sh` block from the release page on the CP host. The web Update Now button is disabled for this case.
* **Modal indicator**: warning chip on the release entry; warning-color Alert at the top of the modal with a "View release" button.

The "tarball asset is present" signal is read by the modal's poller from the GitHub Releases API. No manual flag, no metadata schema — just don't attach a tarball if it's image-only.

---

## Version tracking — three fields

The modal tracks three version identifiers:

| Field | Source | When it changes |
|---|---|---|
| `running_version` | `org.opencontainers.image.version` LABEL baked into the running drift-agent + drift-frontend container images | Every successful `docker compose pull` + recreate (Update now, or install.sh's own compose-up) |
| `install_version` | `INSTALL_VERSION="vX.Y.Z"` line stamped into the tarball's `install.sh` at packaging time, written into `.env` when the operator runs it | Only when the operator re-extracts a new tarball and runs `install.sh` |
| `latest_release_tag` | Latest release tag from `Scope-Creep-Labs/drift` via the GitHub Releases API | When you cut a new release |

The chip at the top of the modal shows **`running_version`** — that's what's actually executing right now. If `install_version` differs (image-only updates applied without a re-install), a secondary line shows the bundle version separately.

---

## All scenarios

### A. Everything current

* `running = install = latest`
* Modal: just the running-version chip, image cards "up to date", Update now disabled (no diff to apply).

### B. Image-only release published; operator hasn't updated yet

* Operator: `running = install = v0.1.16`, `latest = v0.1.17 (image-only)`.
* Modal: chip shows `v0.1.16 → v0.1.17` (info color). "What's new in v0.1.17" banner with `image-only` chip. Image cards "update available". Update now **enabled**.
* Operator clicks Update now → images recreate → `running = v0.1.17`. `install` stays `v0.1.16` (no re-install). Modal: chip shows just `v0.1.17`, all banners gone.

### C. Bundle release published; operator hasn't updated yet

* Operator: `running = install = v0.1.16`, `latest = v0.1.17 (bundle)`.
* Modal: chip shows `v0.1.16 → v0.1.17` (warning color). "What's new in v0.1.17" banner with `bundle` chip. Image cards "update available". **Warning Alert** at the top: "Manual upgrade required for v0.1.17", **Update now disabled**, "View release" button.
* Operator follows the release page instructions: `curl | tar | install.sh`. After install.sh: `install = v0.1.17`, images get pulled to `:latest` (= v0.1.17), `running = v0.1.17`. Modal: clean state.

### D. Multiple image-only updates between bundle releases

* `install = v0.1.16`, `running = v0.1.16`. Then v0.1.17 (image-only), v0.1.18 (image-only), v0.1.19 (image-only) all get published.
* Modal: `v0.1.16 → v0.1.19` (info). Update now picks up the latest pushed images = v0.1.19.
* After Update now: `running = v0.1.19`, `install = v0.1.16` (irrelevant — none of the pending releases needed bundle changes). No bundle banner.

### E. Operator falls behind multiple bundle releases

* `install = v0.1.16`. v0.1.17 (image-only), v0.1.18 (bundle), v0.1.19 (image-only) all get published.
* `bundle_update_available = true` (v0.1.18 has a tarball, and `install < v0.1.18`).
* Modal: warning Alert "Manual upgrade required for v0.1.19", Update now disabled. Re-install jumps the operator straight to v0.1.19 (which includes all the v0.1.18 and v0.1.19 changes).

### F. Mid-update SPA-bundle staleness

* Operator clicks Update now → drift-frontend recreates → new SPA JS is now being served, but the operator's tab is still running the old JS in memory.
* Modal detects the live `running_version` changed since the page was loaded and swaps the **Update now** button for a **Refresh page** button (and shows a green success Alert with a Refresh action). One click → `window.location.reload()` → fresh SPA loads.

---

## Release-author workflow cheat sheet

```bash
# 1. Bump version + commit code changes (or merge PR)

# 2. Decide release kind by what you touched since last release:
#    - Only drift-agent/ or src/ (Python / SPA code)?      → image-only
#    - install.sh / docker-compose.yml / config/*.tmpl?    → bundle

# 3a. Image-only release
./deploy/package-release.sh \
    --publish vX.Y.Z \
    --image-only \
    --notes deploy/release-notes/vX.Y.Z.md

# 3b. Bundle release
./deploy/package-release.sh \
    --publish vX.Y.Z \
    --notes deploy/release-notes/vX.Y.Z.md
```

(`--repo` is unnecessary when releasing into the same repo that holds the source; the script defaults to the current repo. Pass `--repo other/repo` only when publishing to a separate releases repo, e.g. a private-source / public-releases split.)


In both cases the script:

1. Builds `drift-agent` + `drift-frontend` images with `--build-arg VERSION=vX.Y.Z`.
2. Tags both `:vX.Y.Z` and `:latest`.
3. Pushes both tags to GHCR.
4. (Bundle only) Builds the tarball + sha256, stamps `INSTALL_VERSION="vX.Y.Z"` into `install.sh`.
5. Creates the GitHub release with notes, attaching the tarball iff it's a bundle release.

**Do not** push images to GHCR outside of this script. Out-of-band image pushes break the modal's version display (the `running_version` LABEL drifts from any released tag).

---

## Why bundle updates can't be applied via the web

Bundle changes can include:

* New env vars referenced in `docker-compose.yml` — `up -d` against the running compose would still use the old compose and either fail variable interpolation or silently pass blanks.
* New services, new mounts, port-binding changes — invisible to `compose pull` + `compose up` against the old file.
* Config template changes — `config/alertmanager-ntfy.yml.tmpl` etc. — never re-rendered by `compose up`.
* `install.sh` itself — e.g. changes to chown/chmod, host user setup, CA bundle detection.

`docker compose pull && up -d` covers the "new image versions of existing services with the same compose contract" case only. The moment the compose contract changes, you need the new bundle on disk. Web-applied bundle updates would either silently corrupt config or fail in confusing ways.

---

## Discipline for release authors

* Bump the version every time you cut a release — no "patch the latest" force-pushes.
* Never push images outside of `package-release.sh`.
* If a change touches both code AND the bundle, it's a bundle release — don't try to split it.
* If you cut a bundle release and forget to attach the tarball, the modal will treat it as image-only and the operator can apply it via Update now — which won't work for the bundle parts. Re-cut with the tarball.
