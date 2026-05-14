"""Admin surface for Drift Deploy.

Mounted at /api/deploy. Used by the LLM tools layer; protected upstream by
Caddy basic_auth (no app-level auth in v0). The shape mirrors the
spec's data model: devices, apps, app_revisions, deployment_targets.
"""
from __future__ import annotations

import uuid
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from . import bundles, security
from .observability import revision_uploads_total
from .db import session
from .models import App, AppRevision, Device, DeploymentTarget
from .schemas import (
    AppCreate,
    AppOut,
    AppRevisionCreate,
    AppRevisionOut,
    DeploymentTargetOut,
    DeploymentTargetSet,
    DeviceCreate,
    DeviceCreated,
    DeviceOut,
)


router = APIRouter(prefix="/api/deploy", tags=["deploy-admin"])


async def get_db() -> AsyncIterator[AsyncSession]:
    async with session() as s:
        yield s


# ---------- Devices ----------


@router.get("/devices", response_model=list[DeviceOut])
async def list_devices(db: AsyncSession = Depends(get_db)) -> list[DeviceOut]:
    rows = await db.execute(select(Device).order_by(Device.created_at.desc()))
    return [DeviceOut.model_validate(d, from_attributes=True) for d in rows.scalars().all()]


@router.post("/devices", response_model=DeviceCreated, status_code=status.HTTP_201_CREATED)
async def create_device(body: DeviceCreate, db: AsyncSession = Depends(get_db)) -> DeviceCreated:
    existing = await db.execute(select(Device).where(Device.name == body.name))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"device '{body.name}' already exists")
    token = security.generate_bootstrap_token()
    device = Device(name=body.name, bootstrap_token_hash=security.hash_token(token))
    db.add(device)
    await db.commit()
    await db.refresh(device)
    return DeviceCreated(
        device=DeviceOut.model_validate(device, from_attributes=True),
        bootstrap_token=token,
        install_cmd=_render_install_cmd(body.name, token),
    )


@router.delete("/devices/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_device(name: str, db: AsyncSession = Depends(get_db)) -> None:
    row = await db.execute(select(Device).where(Device.name == name))
    device = row.scalar_one_or_none()
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"device '{name}' not found")
    await db.delete(device)
    await db.commit()


def _render_install_cmd(name: str, token: str) -> str:
    # /drift/api/deploy/agent/* is intentionally NOT Caddy-basic-auth-gated
    # (the bootstrap token is the device's credential), so no -u flag is
    # needed here. The token itself is the secret.
    return (
        f"curl -sSL https://drift.example.com/drift/api/deploy/agent/install.sh | "
        f"DEVICE_NAME={name} BOOTSTRAP_TOKEN={token} "
        f"CP_URL=https://drift.example.com/drift/api/deploy sudo -E bash"
    )


# ---------- Apps ----------


@router.get("/apps", response_model=list[AppOut])
async def list_apps(db: AsyncSession = Depends(get_db)) -> list[AppOut]:
    rows = await db.execute(select(App).order_by(App.created_at.desc()))
    return [AppOut.model_validate(a, from_attributes=True) for a in rows.scalars().all()]


@router.post("/apps", response_model=AppOut, status_code=status.HTTP_201_CREATED)
async def create_app(body: AppCreate, db: AsyncSession = Depends(get_db)) -> AppOut:
    existing = await db.execute(select(App).where(App.name == body.name))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"app '{body.name}' already exists")
    app = App(name=body.name)
    db.add(app)
    await db.commit()
    await db.refresh(app)
    return AppOut.model_validate(app, from_attributes=True)


# ---------- App revisions ----------


@router.get("/apps/{name}/revisions", response_model=list[AppRevisionOut])
async def list_revisions(name: str, db: AsyncSession = Depends(get_db)) -> list[AppRevisionOut]:
    app = await _app_by_name(db, name)
    rows = await db.execute(
        select(AppRevision).where(AppRevision.app_id == app.id).order_by(AppRevision.version.desc())
    )
    return [AppRevisionOut.model_validate(r, from_attributes=True) for r in rows.scalars().all()]


@router.post(
    "/apps/{name}/revisions",
    response_model=AppRevisionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_revision(
    name: str, body: AppRevisionCreate, db: AsyncSession = Depends(get_db)
) -> AppRevisionOut:
    if not settings.b2_bucket:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "B2 storage not configured; cannot upload bundles",
        )
    app = await _app_by_name(db, name)
    # Next version is monotonic per app.
    latest = await db.execute(
        select(AppRevision.version)
        .where(AppRevision.app_id == app.id)
        .order_by(AppRevision.version.desc())
        .limit(1)
    )
    next_version = (latest.scalar_one_or_none() or 0) + 1

    data, digest = bundles.pack(body.files)
    bundle_url = bundles.upload_bundle(app.name, next_version, data)

    rev = AppRevision(
        app_id=app.id,
        version=next_version,
        files=body.files,
        bundle_url=bundle_url,
        bundle_sha256=digest,
    )
    db.add(rev)
    await db.commit()
    await db.refresh(rev)
    revision_uploads_total.labels(app=app.name).inc()
    return AppRevisionOut.model_validate(rev, from_attributes=True)


# ---------- Deployment targets ----------


@router.get("/deployments", response_model=list[DeploymentTargetOut])
async def list_deployments(db: AsyncSession = Depends(get_db)) -> list[DeploymentTargetOut]:
    rows = await db.execute(select(DeploymentTarget).order_by(DeploymentTarget.updated_at.desc()))
    return [DeploymentTargetOut.model_validate(t, from_attributes=True) for t in rows.scalars().all()]


@router.post(
    "/deployments",
    response_model=DeploymentTargetOut,
    status_code=status.HTTP_201_CREATED,
)
async def set_deployment(
    body: DeploymentTargetSet, db: AsyncSession = Depends(get_db)
) -> DeploymentTargetOut:
    # Validate references.
    device = (await db.execute(select(Device).where(Device.id == body.device_id))).scalar_one_or_none()
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device not found")
    rev = (await db.execute(select(AppRevision).where(AppRevision.id == body.revision_id))).scalar_one_or_none()
    if rev is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "revision not found")
    if rev.app_id != body.app_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "revision does not belong to that app")

    existing = await db.execute(
        select(DeploymentTarget).where(
            DeploymentTarget.device_id == body.device_id,
            DeploymentTarget.app_id == body.app_id,
        )
    )
    target = existing.scalar_one_or_none()
    if target is None:
        target = DeploymentTarget(
            device_id=body.device_id,
            app_id=body.app_id,
            desired_revision_id=body.revision_id,
            status="pending",
        )
        db.add(target)
    else:
        target.desired_revision_id = body.revision_id
        # Only flip status to pending when desired actually changed.
        if target.current_revision_id != body.revision_id:
            target.status = "pending"
            target.last_error = None
    await db.commit()
    await db.refresh(target)
    return DeploymentTargetOut.model_validate(target, from_attributes=True)


# ---------- helpers ----------


async def _app_by_name(db: AsyncSession, name: str) -> App:
    row = await db.execute(select(App).where(App.name == name))
    app = row.scalar_one_or_none()
    if app is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"app '{name}' not found")
    return app
