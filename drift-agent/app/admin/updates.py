"""Update check + apply for CP-side images.

Polls GHCR's anonymous manifest API for the `:latest` tag of each
tracked image and compares to the digest the local Docker daemon
reports for the same tag. Result is cached for ~15 min so the admin
UI can poll without hammering the registry.

Apply path shells out via /var/run/docker.sock (already mounted into
this container) to `docker pull` + `docker compose up -d` for the CP
services. drift-agent restarting itself is fine — the SPA reconnects
automatically when SSE/WS resume.

Edge-agent image updates are NOT applied from here — that's a per-
device rerun of edge install.sh. We just surface whether the build
context the CP serves has changed since the last drift-agent boot,
so the admin knows a per-device rebuild is recommended.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

log = logging.getLogger(__name__)

# Images we check at GHCR. Each entry maps the friendly name shown in
# the admin UI to:
#   - the image reference (registry/owner/name)
#   - the compose service name (used for `docker compose up -d`)
#   - a description shown alongside the version row.
TRACKED_IMAGES: list[dict] = [
    {
        "name": "drift-agent",
        "image": "ghcr.io/kidproquo/drift-agent",
        "tag": "latest",
        "compose_service": "drift-agent",
        "description": "Backend (FastAPI + agent loop + deploy CP).",
    },
    {
        "name": "drift-frontend",
        "image": "ghcr.io/kidproquo/drift-frontend",
        "tag": "latest",
        "compose_service": "drift-frontend",
        "description": "Web UI (React SPA + nginx).",
    },
]

# Background poll interval. 15 min keeps the GHCR API hits modest
# while still catching new releases within a useful window.
POLL_INTERVAL_SECONDS = 15 * 60


@dataclass
class ImageStatus:
    name: str
    image: str
    tag: str
    compose_service: str
    description: str
    current_digest: Optional[str] = None    # digest of the running container's image
    available_digest: Optional[str] = None  # digest the registry reports for :tag
    # `org.opencontainers.image.version` LABEL on the running container's
    # image, stamped by package-release.sh's --build-arg VERSION=… at
    # build time. Empty for images built before the labelling change.
    current_version: Optional[str] = None
    update_available: bool = False
    last_check: Optional[str] = None        # ISO timestamp of the most recent poll
    error: Optional[str] = None             # populated on poll failure


@dataclass
class ReleaseNote:
    tag: str
    name: str
    body: str           # markdown
    html_url: str
    published_at: str   # ISO timestamp
    # True iff this release has a drift-deploy-*.tar.gz asset attached.
    # The presence of the tarball IS the bundle-change signal — image-
    # only releases (just notes + tag, no tarball) flip this to False
    # and don't trigger the "re-install required" banner.
    has_bundle_changes: bool = True


@dataclass
class UpdateSnapshot:
    checked_at: Optional[str] = None
    images: list[ImageStatus] = field(default_factory=list)
    # Edge-agent files baked into THIS drift-agent image — surfaced so
    # admins know whether a per-device install.sh rerun is needed.
    edge_agent_version: Optional[str] = None
    edge_agent_sha: Optional[str] = None
    # Recent releases from RELEASES_REPO, newest first. Rendered as
    # markdown in the admin UI alongside the digest diff.
    releases: list[ReleaseNote] = field(default_factory=list)


# GitHub repo where release notes live. Tarball releases of the
# single-server installer are published here; the body field of each
# release is markdown that the admin UI renders alongside the digest
# diff. Public repo → anonymous fetch is fine.
RELEASES_REPO = "kidproquo/drift-public"
RELEASES_LIMIT = 5  # how many recent releases to include in the snapshot


_snapshot = UpdateSnapshot()
_poll_task: Optional[asyncio.Task] = None
_apply_lock = asyncio.Lock()


# ---------- GHCR manifest probe ----------

async def _fetch_ghcr_digest(image: str, tag: str) -> str:
    """Return the manifest digest of `image:tag` on ghcr.io.

    Anonymous read works for public packages. The token endpoint returns
    a short-lived bearer; we use it on the manifest call. We accept both
    OCI and Docker v2 manifest media types so multi-arch indexes also
    resolve cleanly.
    """
    repo = image.removeprefix("ghcr.io/")
    async with httpx.AsyncClient(timeout=10) as client:
        # Anonymous token for the specific scope.
        tok_resp = await client.get(
            "https://ghcr.io/token",
            params={"service": "ghcr.io", "scope": f"repository:{repo}:pull"},
        )
        tok_resp.raise_for_status()
        token = tok_resp.json()["token"]

        # Probe the manifest. Use HEAD; the registry returns the digest
        # in Docker-Content-Digest. Accept both index + manifest types.
        accept = ", ".join(
            [
                "application/vnd.oci.image.index.v1+json",
                "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.docker.distribution.manifest.list.v2+json",
                "application/vnd.docker.distribution.manifest.v2+json",
            ]
        )
        man_resp = await client.head(
            f"https://ghcr.io/v2/{repo}/manifests/{tag}",
            headers={"Authorization": f"Bearer {token}", "Accept": accept},
        )
        man_resp.raise_for_status()
        digest = man_resp.headers.get("Docker-Content-Digest")
        if not digest:
            raise RuntimeError("registry returned no Docker-Content-Digest header")
        return digest


# ---------- Local docker inspect ----------

def _running_image_info(compose_service: str) -> tuple[Optional[str], Optional[str]]:
    """Pull the RepoDigest + version-label off the running container.

    Returns (digest, version) where:
      - digest is a `sha256:...` string (the RepoDigest of the running
        container's image, used for comparing against GHCR's
        manifest digest), None if undetectable.
      - version is the `org.opencontainers.image.version` LABEL on the
        running image, None if absent (image built before the label
        scheme).
    """
    container = f"drift-{compose_service.removeprefix('drift-')}"
    try:
        image_id = subprocess.check_output(
            ["docker", "inspect", "--format", "{{.Image}}", container],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode().strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return None, None
    digest: Optional[str] = None
    version: Optional[str] = None
    try:
        out = subprocess.check_output(
            [
                "docker", "inspect", "--format",
                # one shot: RepoDigests JSON + the version label
                '{{json .RepoDigests}}|{{index .Config.Labels "org.opencontainers.image.version"}}',
                image_id,
            ],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode().strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return None, None
    if "|" in out:
        digests_json, version = out.split("|", 1)
        version = version.strip() or None
        try:
            for d in json.loads(digests_json):
                if "@" in d:
                    digest = d.split("@", 1)[1]
                    break
        except json.JSONDecodeError:
            pass
    return digest, version


# ---------- Release notes (GitHub releases for drift-public) ----------

async def _fetch_recent_releases() -> list[ReleaseNote]:
    """Hit the public Releases API. Anonymous is fine for public repos
    (rate limit 60/hr; we poll 4/hr). Returns newest-first.

    has_bundle_changes is inferred from the release's attached assets:
    if a drift-deploy-*.tar.gz is present, the release carries bundle
    changes that require a re-install. Image-only releases (just notes
    + tag) get has_bundle_changes=False and don't trigger the bundle
    banner — the web Update Now button is sufficient for them."""
    url = f"https://api.github.com/repos/{RELEASES_REPO}/releases"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            url,
            params={"per_page": RELEASES_LIMIT},
            headers={"Accept": "application/vnd.github+json"},
        )
        resp.raise_for_status()
    raw = resp.json()
    out: list[ReleaseNote] = []
    for r in raw:
        # Skip drafts; pre-releases are still shown (operator can decide).
        if r.get("draft"):
            continue
        assets = r.get("assets") or []
        has_tarball = any(
            (a.get("name") or "").startswith("drift-deploy-")
            and (a.get("name") or "").endswith(".tar.gz")
            for a in assets
        )
        out.append(
            ReleaseNote(
                tag=r.get("tag_name") or "",
                name=r.get("name") or r.get("tag_name") or "",
                body=r.get("body") or "",
                html_url=r.get("html_url") or "",
                published_at=r.get("published_at") or "",
                has_bundle_changes=has_tarball,
            )
        )
    return out


# ---------- Edge-agent SHA + version (baked into this image) ----------

def _edge_agent_metadata() -> tuple[Optional[str], Optional[str]]:
    """Read AGENT_VERSION + compute SHA for /opt/edge-agent/drift-deploy-agent.sh."""
    import hashlib
    from pathlib import Path

    path = Path("/opt/edge-agent/drift-deploy-agent.sh")
    if not path.is_file():
        return None, None
    content = path.read_bytes()
    sha = hashlib.sha256(content).hexdigest()[:12]
    # Pull AGENT_VERSION=... line out of the script body.
    version = None
    for line in content.decode(errors="ignore").splitlines():
        if line.startswith("AGENT_VERSION="):
            version = line.split("=", 1)[1].strip().strip('"').strip("'")
            break
    return version, sha


# ---------- Poll loop ----------

async def _poll_once() -> None:
    now = datetime.now(timezone.utc).isoformat()
    statuses: list[ImageStatus] = []
    for entry in TRACKED_IMAGES:
        s = ImageStatus(
            name=entry["name"],
            image=entry["image"],
            tag=entry["tag"],
            compose_service=entry["compose_service"],
            description=entry["description"],
            last_check=now,
        )
        try:
            s.current_digest, s.current_version = _running_image_info(entry["compose_service"])
            s.available_digest = await _fetch_ghcr_digest(entry["image"], entry["tag"])
            s.update_available = bool(
                s.current_digest
                and s.available_digest
                and s.current_digest != s.available_digest
            )
        except Exception as e:  # noqa: BLE001
            s.error = str(e)
            log.warning("admin/updates: poll failed for %s: %s", entry["name"], e)
        statuses.append(s)
    edge_v, edge_sha = _edge_agent_metadata()
    # Release notes are best-effort — a transient GitHub outage shouldn't
    # blank out the digest comparison the admin actually needs.
    try:
        releases = await _fetch_recent_releases()
    except Exception as e:  # noqa: BLE001
        log.warning("admin/updates: release notes fetch failed: %s", e)
        releases = _snapshot.releases  # keep the previous cached set
    _snapshot.checked_at = now
    _snapshot.images = statuses
    _snapshot.edge_agent_version = edge_v
    _snapshot.edge_agent_sha = edge_sha
    _snapshot.releases = releases


async def _poll_loop() -> None:
    # Do one poll immediately on startup so the UI has something to
    # show as soon as the page loads.
    await _poll_once()
    while True:
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        with suppress(Exception):
            await _poll_once()


def start_updates_poller() -> None:
    global _poll_task
    if _poll_task is None or _poll_task.done():
        _poll_task = asyncio.create_task(_poll_loop(), name="drift-admin-updates")


async def stop_updates_poller() -> None:
    global _poll_task
    if _poll_task and not _poll_task.done():
        _poll_task.cancel()
        with suppress(asyncio.CancelledError):
            await _poll_task


# ---------- Public surface ----------

def _version_tuple(tag: str) -> tuple:
    """Parse a release tag like "v0.1.12" or "0.1.12-rc1" into a comparable
    tuple of ints. Returns () if the tag doesn't yield at least one number,
    which forces the caller to skip the comparison rather than guess."""
    s = (tag or "").lstrip("v").lstrip("V")
    parts: list[int] = []
    for p in s.split("."):
        digits = ""
        for c in p:
            if c.isdigit():
                digits += c
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def get_snapshot() -> dict:
    """Snapshot of the current poll result, JSON-serializable."""
    # Lazy import so the module doesn't fail to load if config is broken.
    from ..config import settings as _settings

    installed = _settings.install_version or ""
    latest_release_tag = (_snapshot.releases[0].tag if _snapshot.releases else "") or ""
    iv = _version_tuple(installed)
    lv = _version_tuple(latest_release_tag)

    # image_update_pending: the running images are at a lower version
    # than the latest release. This is what the "Update now" button
    # addresses. Computed inline below after running_version is set.

    # bundle_update_available: at least one PENDING release (newer than
    # the installed tarball) carries a tarball asset → a re-install is
    # required to pick up install.sh / compose / config changes. Image-
    # only releases don't trigger this.
    bundle_update = False
    if iv and lv:
        for r in _snapshot.releases:
            rv = _version_tuple(r.tag)
            if rv and iv < rv and r.has_bundle_changes:
                bundle_update = True
                break

    # running_version: the effective release the operator is on,
    # derived from the image LABELs. Use MAX across services rather
    # than MIN — when smart-build skips an unchanged image for a
    # release, the LABEL on that image intentionally stays at the
    # release where its source LAST changed (not "I'm behind"). The
    # highest label across services is therefore the most recent
    # release the operator has applied any part of, and equals the
    # latest release tag whenever they're fully current. None when
    # no images carry the label (images built before this scheme).
    versions = [
        _version_tuple(i.current_version)
        for i in _snapshot.images
        if i.current_version
    ]
    running_version: Optional[str] = None
    if versions and all(versions):
        highest = max(versions)
        for i in _snapshot.images:
            if i.current_version and _version_tuple(i.current_version) == highest:
                running_version = i.current_version
                break

    # image_update_pending: running images are at a lower version than
    # the latest release. Computed here (after running_version is set).
    rv = _version_tuple(running_version) if running_version else ()
    image_update_pending = bool(rv and lv and rv < lv)

    # has_newer_release: any kind of newer release the operator hasn't
    # fully applied yet. Drives the "What's new" banner. False means
    # the operator's effective state IS the latest release.
    has_newer_release = image_update_pending or bundle_update

    return {
        "checked_at": _snapshot.checked_at,
        "install_version": installed or None,
        "running_version": running_version,
        "latest_release_tag": latest_release_tag or None,
        "has_newer_release": has_newer_release,
        "image_update_pending": image_update_pending,
        "bundle_update_available": bundle_update,
        "images": [asdict(i) for i in _snapshot.images],
        "edge_agent": {
            "version": _snapshot.edge_agent_version,
            "sha": _snapshot.edge_agent_sha,
            # Image rebuild on devices is operator-driven (rerun
            # install.sh); we surface a hint, never auto-apply.
            "note": (
                "Edge-agent script + terminal-bridge.py auto-update on "
                "next check-in. Per-device install.sh rerun only needed "
                "for deep image changes (Dockerfile, apk packages)."
            ),
        },
        "releases": [asdict(r) for r in _snapshot.releases],
    }


async def trigger_check() -> dict:
    """Force an immediate poll. Returns the resulting snapshot."""
    await _poll_once()
    return get_snapshot()


async def apply_cp_updates() -> dict:
    """Pull + recreate drift-agent and drift-frontend.

    Serialized via _apply_lock — concurrent presses of the "Update now"
    button just no-op the second caller. The script returns the apply
    log + the new snapshot. drift-agent recreates itself in the same
    `docker compose up -d`; the HTTP response races the container
    restart, so callers should treat a connection drop as success-likely
    and re-poll once the SPA reconnects.
    """
    if _apply_lock.locked():
        return {"error": "another update is already in progress"}
    async with _apply_lock:
        # Pull is just an image fetch — no compose file needed, talks
        # straight to the daemon.
        env = os.environ.copy()
        pull_log = []
        for entry in TRACKED_IMAGES:
            ref = f"{entry['image']}:{entry['tag']}"
            p = subprocess.run(
                ["docker", "pull", ref],
                capture_output=True, text=True, timeout=300, env=env,
            )
            pull_log.append(f"$ docker pull {ref}\n{p.stdout}{p.stderr}")
            if p.returncode != 0:
                return {
                    "error": f"docker pull failed for {ref}",
                    "pull_output": "\n".join(pull_log),
                }

        # Recreate via compose. The install dir is bind-mounted at the
        # SAME path inside this container as on the host (see compose
        # `volumes:` block) so we can use one consistent path for
        # `compose -f` AND `--project-directory`. The daemon also sees
        # bind-mount sources (./config/alerts → DEPLOY_DIR/config/...)
        # on that identical path on the host — no translation needed.
        deploy_dir = os.environ.get("DEPLOY_DIR")
        if not deploy_dir:
            return {
                "error": "DEPLOY_DIR env var not set on drift-agent — "
                         "rerun install.sh to update docker-compose.yml.",
                "pull_output": "\n".join(pull_log),
            }
        if not os.path.isdir(deploy_dir):
            return {
                "error": f"DEPLOY_DIR={deploy_dir} not mounted into drift-agent. "
                         "Recreate the container (docker compose up -d drift-agent) "
                         "after upgrading docker-compose.yml to v0.1.15+.",
                "pull_output": "\n".join(pull_log),
            }
        compose_files = ["-f", f"{deploy_dir}/docker-compose.yml"]
        external = f"{deploy_dir}/docker-compose.external.yml"
        if os.path.exists(external):
            compose_files += ["-f", external]

        services = [e["compose_service"] for e in TRACKED_IMAGES]

        # `docker compose up -d` for drift-agent would kill the process
        # running the command (this one) mid-recreation. Suicide.
        # Spawn a DETACHED helper container that survives drift-agent's
        # death and finishes the recreate. We reuse drift-agent's own
        # image because it already has the docker CLI + compose plugin
        # installed — no second image to maintain.
        helper_name = "drift-updater-helper"
        # Remove any stale helper (e.g. from a previous run that
        # crashed before --rm could clean up).
        subprocess.run(["docker", "rm", "-f", helper_name],
                       capture_output=True, timeout=10)

        helper_image = os.environ.get("HOSTNAME", "")  # docker container id
        # Fall back to a known tag if we can't introspect the running image.
        try:
            helper_image = subprocess.check_output(
                ["docker", "inspect", "--format", "{{.Config.Image}}", "drift-agent"],
                timeout=5, env=env,
            ).decode().strip()
        except subprocess.SubprocessError:
            helper_image = "ghcr.io/kidproquo/drift-agent:latest"

        # Brief sleep so this HTTP response can return cleanly before
        # the helper starts churning the parent container.
        helper_script = (
            f"sleep 3 && "
            f"docker compose {' '.join(compose_files)} "
            f"--project-directory {deploy_dir} "
            f"up -d --no-deps {' '.join(services)}"
        )

        helper_run = subprocess.run(
            [
                "docker", "run", "-d",
                "--rm",
                "--name", helper_name,
                "--user", "0:0",  # avoid the docker.sock group dance
                "-v", "/var/run/docker.sock:/var/run/docker.sock",
                "-v", f"{deploy_dir}:{deploy_dir}:ro",
                "--entrypoint", "sh",
                helper_image,
                "-c", helper_script,
            ],
            capture_output=True, text=True, timeout=30, env=env,
        )
        # Don't await the helper's compose-up here — the helper is
        # detached and runs ~5-15s after our response goes out. The
        # SPA polls back for the result.
        return {
            "applied": services,
            "pull_output": "\n".join(pull_log)[-2000:],
            "helper_returncode": helper_run.returncode,
            "helper_output": (helper_run.stdout + helper_run.stderr)[-500:],
            "message": (
                "recreate dispatched to detached helper container; "
                "drift-agent will restart in a few seconds, then the modal "
                "will repoll for the new digests"
            ),
        }
