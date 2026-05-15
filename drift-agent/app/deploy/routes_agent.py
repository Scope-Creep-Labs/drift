"""Agent surface — what the edge bash agent calls every 30s.

Auth is a bearer token (the bootstrap_token from device creation) validated
against the device's stored hash.
"""
from __future__ import annotations

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
BUILD_CONTEXT_FILES = ("Dockerfile", "drift-deploy-agent.sh")

from . import bundles
from .auth import authenticate_device, extract_bearer
from .db import session
from .models import App, AppRevision, DeploymentTarget
from .observability import apply_transitions_total, check_ins_total
from .schemas import AgentCheckIn, AgentCheckInResponse, DesiredApp


router = APIRouter(prefix="/api/deploy/agent", tags=["deploy-agent"])


async def get_db() -> AsyncIterator[AsyncSession]:
    async with session() as s:
        yield s


@router.get("/install.sh")
async def install_script() -> FileResponse:
    """Installer the operator pipes into `sudo -E bash`. Runs docker build
    + docker run; no systemd, no host-side compose plugin required."""
    return _serve_edge_file("install.sh", media_type="text/x-shellscript")


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

    # Update current_revision_id for any (device, app) pair the agent reports.
    if body.current_revisions:
        # Resolve app names → ids in one go.
        names = list(body.current_revisions.keys())
        rows = await db.execute(select(App).where(App.name.in_(names)))
        name_to_id = {a.name: a.id for a in rows.scalars().all()}

        for app_name, current_rev_id in body.current_revisions.items():
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
            target.current_revision_id = current_rev_id
            if target.desired_revision_id == current_rev_id:
                target.status = "healthy"
                target.last_error = None
            if prior_status != target.status:
                apply_transitions_total.labels(
                    from_status=prior_status, to_status=target.status
                ).inc()

    # Build desired-state response: every target with desired != current.
    targets_rows = await db.execute(
        select(DeploymentTarget, App, AppRevision)
        .join(App, App.id == DeploymentTarget.app_id)
        .join(AppRevision, AppRevision.id == DeploymentTarget.desired_revision_id)
        .where(DeploymentTarget.device_id == device.id)
    )
    desired: list[DesiredApp] = []
    for target, app, rev in targets_rows.all():
        if target.current_revision_id == target.desired_revision_id:
            continue
        if not rev.bundle_url or not rev.bundle_sha256:
            # Should not happen — revision creation always uploads — but skip
            # rather than hand the agent a broken instruction.
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
                revision_id=rev.id,
                bundle_url=url,
                bundle_sha256=rev.bundle_sha256,
            )
        )

    await db.commit()
    return AgentCheckInResponse(desired=desired)
