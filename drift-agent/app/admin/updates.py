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

def _running_image_digest(compose_service: str) -> Optional[str]:
    """Pull the RepoDigest of the running container for a compose
    service. We look up by container_name=drift-<service>; we use
    docker inspect via the socket because Python's docker SDK isn't
    installed in the runtime image."""
    container = f"drift-{compose_service.removeprefix('drift-')}"
    try:
        out = subprocess.check_output(
            ["docker", "inspect", "--format", "{{json .Image}} {{json .Config.Image}}", container],
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).decode().strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    # First token = the image's content-addressed ID (sha256:...).
    # We want the RepoDigest of the tag that container was started from.
    # Use a second inspect on the image id to get RepoDigests.
    parts = out.split(" ", 1)
    if not parts:
        return None
    image_id = parts[0].strip('"')
    try:
        digests_json = subprocess.check_output(
            ["docker", "inspect", "--format", "{{json .RepoDigests}}", image_id],
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).decode().strip()
        digests = json.loads(digests_json)
    except (subprocess.SubprocessError, FileNotFoundError, json.JSONDecodeError):
        return None
    # Pick the one that matches our tracked image ref. Each entry looks
    # like ghcr.io/owner/name@sha256:....
    for d in digests:
        if "@" in d:
            return d.split("@", 1)[1]
    return None


# ---------- Release notes (GitHub releases for drift-public) ----------

async def _fetch_recent_releases() -> list[ReleaseNote]:
    """Hit the public Releases API. Anonymous is fine for public repos
    (rate limit 60/hr; we poll 4/hr). Returns newest-first."""
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
        out.append(
            ReleaseNote(
                tag=r.get("tag_name") or "",
                name=r.get("name") or r.get("tag_name") or "",
                body=r.get("body") or "",
                html_url=r.get("html_url") or "",
                published_at=r.get("published_at") or "",
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
            s.current_digest = _running_image_digest(entry["compose_service"])
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

def get_snapshot() -> dict:
    """Snapshot of the current poll result, JSON-serializable."""
    # Lazy import so the module doesn't fail to load if config is broken.
    from ..config import settings as _settings
    return {
        "checked_at": _snapshot.checked_at,
        "install_version": _settings.install_version or None,
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
        up = subprocess.run(
            [
                "docker", "compose",
                *compose_files,
                "--project-directory", deploy_dir,
                "up", "-d", "--no-deps", *services,
            ],
            capture_output=True, text=True, timeout=120, env=env,
        )
        # Refresh the snapshot — note this might race the recreate; the
        # next scheduled poll catches up either way.
        with suppress(Exception):
            await _poll_once()

        return {
            "applied": services,
            "pull_output": "\n".join(pull_log)[-2000:],
            "up_returncode": up.returncode,
            "up_output": (up.stdout + up.stderr)[-2000:],
            "snapshot": get_snapshot(),
        }
