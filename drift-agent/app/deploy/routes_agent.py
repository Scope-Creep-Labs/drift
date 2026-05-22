"""Agent surface — what the edge bash agent calls every 30s.

Auth is a bearer token (the bootstrap_token from device creation) validated
against the device's stored hash.
"""
from __future__ import annotations

import base64
import hashlib
import io
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


EDGE_AGENT_DIR = Path("/opt/edge-agent")

# Files included in the build context the device pulls to docker build the
# agent image locally. Tiny — keeps install.sh dependency-free of a registry.
BUILD_CONTEXT_FILES = ("Dockerfile", "drift-deploy-agent.sh", "terminal-bridge.py")


_agent_target_sha_cache: str | None = None


def _agent_target_sha() -> str:
    """12-char prefix of the canonical drift-deploy-agent.sh's SHA-256.
    Computed once at first call; the value is baked into the image so it
    only changes when drift-agent is rebuilt (which is when a new
    agent.sh ships)."""
    global _agent_target_sha_cache
    if _agent_target_sha_cache is None:
        path = EDGE_AGENT_DIR / "drift-deploy-agent.sh"
        if path.is_file():
            with open(path, "rb") as f:
                _agent_target_sha_cache = hashlib.sha256(f.read()).hexdigest()[:12]
        else:
            _agent_target_sha_cache = ""
    return _agent_target_sha_cache

from . import bundles, secrets as crypto
from .auth import authenticate_device, extract_bearer
from .db import session
from .models import App, AppRevision, DeploymentTarget, RegistryCredential
from .observability import apply_transitions_total, check_ins_total
from .schemas import AgentCheckIn, AgentCheckInResponse, DesiredApp

from ..config import settings as _settings


router = APIRouter(prefix="/api/deploy/agent", tags=["deploy-agent"])


async def get_db() -> AsyncIterator[AsyncSession]:
    async with session() as s:
        yield s


@router.get("/install.sh")
async def install_script() -> FileResponse:
    """Installer the operator pipes into `sudo -E bash`. Runs docker build
    + docker run; no systemd, no host-side compose plugin required."""
    return _serve_edge_file("install.sh", media_type="text/x-shellscript")


@router.get("/agent.sh")
async def agent_script() -> FileResponse:
    """Latest reconcile-loop script. Agents fetch this at container start
    (the bootstrapper at the top of the script) and exec into it if it
    differs from the in-image baseline. Powers the self-update mechanism;
    no Caddy basic_auth on this path so devices can pull it cleanly."""
    return _serve_edge_file("drift-deploy-agent.sh", media_type="text/x-shellscript")


@router.get("/build-context.tar")
async def build_context() -> StreamingResponse:
    """Build context (Dockerfile + agent.sh) the installer fetches and
    `docker build`s on the device. Tiny tarball; alpine base is fast."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name in BUILD_CONTEXT_FILES:
            path = EDGE_AGENT_DIR / name
            if not path.is_file():
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    f"build context missing file '{name}'",
                )
            tar.add(str(path), arcname=name)
    data = buf.getvalue()
    return StreamingResponse(iter([data]), media_type="application/x-tar")


def _serve_edge_file(name: str, media_type: str) -> FileResponse:
    path = EDGE_AGENT_DIR / name
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"edge-agent file '{name}' not bundled")
    return FileResponse(path, media_type=media_type, filename=name)


@router.post("/check-in", response_model=AgentCheckInResponse)
async def check_in(
    body: AgentCheckIn,
    bearer: str = Depends(extract_bearer),
    db: AsyncSession = Depends(get_db),
) -> AgentCheckInResponse:
    try:
        device = await authenticate_device(body.device_name, bearer, db)
    except HTTPException:
        check_ins_total.labels(result="unauthorized").inc()
        raise
    check_ins_total.labels(result="ok").inc()

    # Update liveness + agent_version + group_id (best-effort).
    device.last_seen = datetime.now(timezone.utc)
    device.agent_version = body.agent_version
    if body.group_id is not None:
        device.group_id = body.group_id
    if device.status != "online":
        device.status = "online"
    # Overwrite identity facts when the agent reports them (every ~10min
    # tick on v0.5.3+). Absent means the agent didn't include facts on
    # this check-in — keep the prior snapshot.
    if body.facts is not None:
        device.facts = body.facts

    # Merge per-app facts the agent reports: current revisions + recent
    # apply errors. Either or both may be empty.
    all_apps = set(body.current_revisions) | set(body.apply_errors)
    if all_apps:
        rows = await db.execute(select(App).where(App.name.in_(all_apps)))
        name_to_id = {a.name: a.id for a in rows.scalars().all()}

        for app_name in all_apps:
            app_id = name_to_id.get(app_name)
            if app_id is None:
                continue
            target_row = await db.execute(
                select(DeploymentTarget).where(
                    DeploymentTarget.device_id == device.id,
                    DeploymentTarget.app_id == app_id,
                )
            )
            target = target_row.scalar_one_or_none()
            if target is None:
                continue
            prior_status = target.status
            current_rev_id = body.current_revisions.get(app_name)
            err = body.apply_errors.get(app_name)

            if current_rev_id is not None:
                target.current_revision_id = current_rev_id

            if err:
                # Don't double-count once the target is already paused.
                # The edge agent's state.json holds apply_errors stickily
                # — every check-in re-reports the same error until a
                # successful apply or an explicit remove clears it. If we
                # incremented unconditionally, paused_retries targets
                # would climb attempts to infinity while never actually
                # being retried.
                if target.status == "paused_retries":
                    target.last_error = err
                else:
                    # Fresh failure — increment. Previous "only when error
                    # TEXT changes" guard was wrong (Docker's error wording
                    # varies slightly between identical underlying failures,
                    # which once let attempts grow past 800).
                    target.attempts += 1
                    target.last_error = err
                    if target.attempts >= target.max_retries:
                        target.status = "paused_retries"
                    else:
                        target.status = "failed"
            elif current_rev_id is not None and target.desired_revision_id == current_rev_id:
                # Healthy: agent confirms current == desired and no error.
                target.status = "healthy"
                target.last_error = None
                target.attempts = 0

            if prior_status != target.status:
                apply_transitions_total.labels(
                    from_status=prior_status, to_status=target.status
                ).inc()

    # Walk all targets for this device, deciding per target whether to
    # emit a deploy or remove instruction, OR delete the row outright
    # because the removal lifecycle is complete.
    targets_rows = await db.execute(
        select(DeploymentTarget, App)
        .join(App, App.id == DeploymentTarget.app_id)
        .where(DeploymentTarget.device_id == device.id)
    )
    desired: list[DesiredApp] = []
    for target, app in targets_rows.all():
        # Lifecycle: NULL desired_revision_id = "marked for removal".
        # We keep the target row even after removal completes so the
        # history of what ran where remains queryable; the `status`
        # column carries the tombstone (`removing` → `removed`).
        if target.desired_revision_id is None:
            if app.name in body.current_revisions:
                # Server intends removal; agent still has it running.
                # Tell the agent to stop. No bundle is shipped.
                desired.append(DesiredApp(app=app.name, action="remove"))
            else:
                # Agent confirmed it's no longer running this app
                # (omitted from current_revisions). Lock in the removed
                # state but keep the row for audit.
                if target.status != "removed":
                    target.status = "removed"
                    target.current_revision_id = None
                    target.last_error = None
            continue

        # Restart signal takes precedence over deploy reconciliation:
        # if the operator asked for a restart, ship that even when the
        # target is otherwise in steady state. Optimistic clear — if
        # the restart fails on the agent, the operator re-issues. The
        # alternative (waiting for an ack) makes the loop more complex
        # for marginal benefit.
        if target.pending_restart:
            desired.append(
                DesiredApp(
                    app=app.name,
                    action="restart",
                    revision_id=target.current_revision_id,
                )
            )
            target.pending_restart = False
            continue

        # Deploy path (existing logic): emit only when desired != current.
        if target.current_revision_id == target.desired_revision_id:
            continue

        # Retry-budget gate: once attempts hit max_retries, the target is
        # paused and we don't ship the bundle anymore. The agent stops
        # retrying because it never sees the desired state. To resume,
        # operator calls retry_deployment (resets attempts) or pushes a
        # new revision (also resets attempts in the admin endpoint).
        if target.status == "paused_retries":
            continue
        rev = (await db.execute(
            select(AppRevision).where(AppRevision.id == target.desired_revision_id)
        )).scalar_one_or_none()
        if rev is None or not rev.bundle_url or not rev.bundle_sha256:
            # Shouldn't happen — revision creation always uploads — but
            # skip rather than hand the agent a broken instruction.
            continue
        try:
            url = bundles.presign_get(rev.bundle_url, expires_in=600)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"could not presign bundle: {e}",
            )
        desired.append(
            DesiredApp(
                app=app.name,
                action="deploy",
                revision_id=rev.id,
                bundle_url=url,
                bundle_sha256=rev.bundle_sha256,
            )
        )

    # Registry credentials: stored encrypted per (registry, group_id)
    # on the CP. Each device only sees creds for its own group, so a
    # client-x device can't read a client-y registry secret even if it
    # were briefly compromised. Decrypted here, repackaged into the
    # docker config.json auths shape so the agent drops it into
    # /root/.docker/config.json verbatim. Skipped when the secrets
    # subsystem is disabled OR the device hasn't been assigned a group
    # (older commission flow predating group_id).
    creds_map: dict[str, dict[str, str]] = {}
    if _settings.secrets_enabled and device.group_id:
        creds_rows = (
            await db.execute(
                select(RegistryCredential).where(
                    RegistryCredential.group_id == device.group_id
                )
            )
        ).scalars().all()
        for c in creds_rows:
            try:
                password = crypto.decrypt(c.password_encrypted)
            except RuntimeError:
                # Stale row encrypted under a rotated key. Skip rather
                # than crashing the whole check-in; operator can
                # re-enter the credential via the UI.
                continue
            auth = base64.b64encode(f"{c.username}:{password}".encode()).decode()
            creds_map[c.registry] = {"auth": auth}

    # Pending terminal sessions for this device. Imported lazily to
    # avoid a circular at module load (terminal.py also imports from
    # this package). The agent forks one terminal-bridge.py per id.
    from .terminal import _get_pending_for_device

    pending_sessions = await _get_pending_for_device(db, device.id)

    await db.commit()
    return AgentCheckInResponse(
        desired=desired,
        agent_target_sha=_agent_target_sha(),
        registry_credentials=creds_map,
        pending_sessions=pending_sessions,
    )
