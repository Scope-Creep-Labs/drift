"""Admin surface for Drift Deploy.

Mounted at /api/deploy. Auth: every endpoint requires an authenticated
user (session cookie via the /api/auth surface). Role checks per
endpoint:
  - GET (reads): require user (observe sufficient). Lists filter by
    user's allowed groups; admins see all.
  - POST/DELETE deploy-state mutations: require role >= deploy AND the
    target must be in the user's groups (admins bypass).
  - Registry creds: admin-only (fleet-wide secrets).
"""
from __future__ import annotations

import uuid
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..users.deps import UserContext, get_current_user, require_role
from . import bundles, secrets as crypto, security
from .observability import revision_uploads_total
from .db import session
from .models import App, AppRevision, Device, DeploymentTarget, RegistryCredential
from .schemas import (
    AppCreate,
    AppOut,
    AppRevisionCreate,
    AppRevisionDetail,
    AppRevisionOut,
    DeploymentTargetOut,
    DeploymentTargetSet,
    DeviceCreate,
    DeviceCreated,
    DeviceOut,
    RegistryCredentialOut,
    RegistryCredentialSet,
)


router = APIRouter(prefix="/api/deploy", tags=["deploy-admin"])


def _check_group_access(user: UserContext, group_id: str | None) -> None:
    """Raise 403 if the user can't act on the given group. Admins bypass."""
    if user.is_admin:
        return
    if group_id is None or not user.has_group(group_id):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"you don't have access to group '{group_id or '<none>'}'",
        )


async def get_db() -> AsyncIterator[AsyncSession]:
    async with session() as s:
        yield s


# ---------- Devices ----------


@router.get("/devices", response_model=list[DeviceOut])
async def list_devices(
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[DeviceOut]:
    q = select(Device).order_by(Device.created_at.desc())
    if not user.is_admin:
        # Filter to the user's groups. Devices with no group are hidden
        # for non-admins; admins always see them.
        q = q.where(Device.group_id.in_(user.groups))
    rows = await db.execute(q)
    return [DeviceOut.model_validate(d, from_attributes=True) for d in rows.scalars().all()]


@router.post("/devices", response_model=DeviceCreated, status_code=status.HTTP_201_CREATED)
async def create_device(
    body: DeviceCreate,
    user: UserContext = Depends(require_role("deploy")),
    db: AsyncSession = Depends(get_db),
) -> DeviceCreated:
    # Note: device.group_id isn't on DeviceCreate (set later by the agent
    # via check-in). Deploy-role users can commission; admins can too.
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
async def delete_device(
    name: str,
    user: UserContext = Depends(require_role("deploy")),
    db: AsyncSession = Depends(get_db),
) -> None:
    row = await db.execute(select(Device).where(Device.name == name))
    device = row.scalar_one_or_none()
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"device '{name}' not found")
    _check_group_access(user, device.group_id)
    await db.delete(device)
    await db.commit()


def _render_install_cmd(name: str, token: str) -> str:
    # /drift/api/deploy/agent/* is intentionally NOT Caddy-basic-auth-gated
    # (the bootstrap token is the device's credential), so no -u flag is
    # needed here. The token itself is the secret.
    return (
        f"curl -sSL https://drift.example.com/drift/api/deploy/agent/install.sh | "
        f"DEVICE_NAME={name} BOOTSTRAP_TOKEN={token} "
        f"CP_URL=https://drift.example.com/drift/api/deploy "
        f"GROUP_ID=CHOOSE_ONE_OF=cloud|edge|client-x|prod sudo -E bash"
    )


# ---------- Apps ----------


@router.get("/apps", response_model=list[AppOut])
async def list_apps(
    _user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AppOut]:
    rows = await db.execute(select(App).order_by(App.created_at.desc()))
    return [AppOut.model_validate(a, from_attributes=True) for a in rows.scalars().all()]


@router.post("/apps", response_model=AppOut, status_code=status.HTTP_201_CREATED)
async def create_app(
    body: AppCreate,
    _user: UserContext = Depends(require_role("deploy")),
    db: AsyncSession = Depends(get_db),
) -> AppOut:
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
async def list_revisions(
    name: str,
    _user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AppRevisionOut]:
    app = await _app_by_name(db, name)
    rows = await db.execute(
        select(AppRevision).where(AppRevision.app_id == app.id).order_by(AppRevision.version.desc())
    )
    return [AppRevisionOut.model_validate(r, from_attributes=True) for r in rows.scalars().all()]


@router.get("/apps/{name}/revisions/{version}", response_model=AppRevisionDetail)
async def get_revision(
    name: str,
    version: str,
    _user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AppRevisionDetail:
    """Single revision including its full file map. The list endpoint above
    strips files for bulk-fetch efficiency; this one is intentionally
    detailed for the edit-app modal. `version` accepts an integer or the
    literal 'latest'."""
    app = await _app_by_name(db, name)
    q = select(AppRevision).where(AppRevision.app_id == app.id)
    if version == "latest":
        q = q.order_by(AppRevision.version.desc()).limit(1)
    else:
        try:
            q = q.where(AppRevision.version == int(version))
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"version must be an integer or 'latest', got '{version}'")
    rev = (await db.execute(q)).scalar_one_or_none()
    if rev is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"revision '{version}' of '{name}' not found")
    return AppRevisionDetail.model_validate(rev, from_attributes=True)


@router.post(
    "/apps/{name}/revisions",
    response_model=AppRevisionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_revision(
    name: str,
    body: AppRevisionCreate,
    _user: UserContext = Depends(require_role("deploy")),
    db: AsyncSession = Depends(get_db),
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
async def list_deployments(
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[DeploymentTargetOut]:
    q = select(DeploymentTarget).order_by(DeploymentTarget.updated_at.desc())
    if not user.is_admin:
        # Join to Device to filter by the device's group.
        q = (
            select(DeploymentTarget)
            .join(Device, Device.id == DeploymentTarget.device_id)
            .where(Device.group_id.in_(user.groups))
            .order_by(DeploymentTarget.updated_at.desc())
        )
    rows = await db.execute(q)
    return [DeploymentTargetOut.model_validate(t, from_attributes=True) for t in rows.scalars().all()]


@router.post(
    "/deployments",
    response_model=DeploymentTargetOut,
    status_code=status.HTTP_201_CREATED,
)
async def set_deployment(
    body: DeploymentTargetSet,
    user: UserContext = Depends(require_role("deploy")),
    db: AsyncSession = Depends(get_db),
) -> DeploymentTargetOut:
    # Validate references.
    device = (await db.execute(select(Device).where(Device.id == body.device_id))).scalar_one_or_none()
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device not found")
    _check_group_access(user, device.group_id)
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
            # If the caller specified a cap, use it; otherwise let the
            # column default (5) take effect.
            **({"max_retries": body.max_retries} if body.max_retries else {}),
        )
        db.add(target)
    else:
        prior_revision = target.desired_revision_id
        target.desired_revision_id = body.revision_id
        if body.max_retries is not None:
            target.max_retries = body.max_retries
        # Push to a fresh deploy attempt when: revision changed OR caller
        # set a new max_retries (presumably operator is bumping the cap
        # to give a previously-paused deployment more chances) OR target
        # is paused_retries (this PUT counts as an explicit resume).
        revision_changed = prior_revision != body.revision_id
        is_paused = target.status == "paused_retries"
        if revision_changed or is_paused:
            target.status = "pending"
            target.last_error = None
            target.attempts = 0
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


# ---------- Registry credentials ----------


def _require_secrets_enabled() -> None:
    if not settings.secrets_enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "secrets subsystem disabled — set DRIFT_SECRET_KEY",
        )


@router.get("/registry-creds", response_model=list[RegistryCredentialOut])
async def list_registry_creds(
    _user: UserContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> list[RegistryCredentialOut]:
    rows = await db.execute(
        select(RegistryCredential).order_by(RegistryCredential.registry)
    )
    return [
        RegistryCredentialOut.model_validate(c, from_attributes=True)
        for c in rows.scalars().all()
    ]


@router.put("/registry-creds", response_model=RegistryCredentialOut)
async def upsert_registry_creds(
    body: RegistryCredentialSet,
    _user: UserContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> RegistryCredentialOut:
    _require_secrets_enabled()
    encrypted = crypto.encrypt(body.password)
    existing = (
        await db.execute(
            select(RegistryCredential).where(RegistryCredential.registry == body.registry)
        )
    ).scalar_one_or_none()
    if existing is None:
        row = RegistryCredential(
            registry=body.registry,
            username=body.username,
            password_encrypted=encrypted,
        )
        db.add(row)
    else:
        existing.username = body.username
        existing.password_encrypted = encrypted
        row = existing
    await db.commit()
    await db.refresh(row)
    return RegistryCredentialOut.model_validate(row, from_attributes=True)


# `registry:path` lets the registry name contain slashes (e.g. an
# index URL like "https://index.docker.io/v1/"). The client should
# still URL-encode the value to keep the router happy with unusual
# characters.
@router.delete("/registry-creds/{registry:path}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_registry_creds(
    registry: str,
    _user: UserContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> None:
    row = (
        await db.execute(
            select(RegistryCredential).where(RegistryCredential.registry == registry)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"no credentials for registry '{registry}'"
        )
    await db.delete(row)
    await db.commit()
