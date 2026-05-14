"""Drift Deploy LLM tools.

The agent talks to the deploy subsystem in-process (same FastAPI app),
not over HTTP — we just import the SQLAlchemy models and reuse the
bundle helpers. This avoids re-doing auth, gets nicer errors, and
shares the live connection pool.

Tools fall into three buckets:

  - **Discovery** (read-only): list_devices, list_apps, list_app_revisions,
    list_deployments, get_device.
  - **Lifecycle / commissioning**: commission_device (returns the
    bootstrap token + install one-liner), delete_device.
  - **Application + deployment**: create_app, propose_app_revision
    (preview), apply_app_revision (writes + uploads), deploy_revision
    (sets desired state). All app/rev/deploy mutators follow the same
    propose-then-apply pattern as the alert tools.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from ..config import settings
from ..deploy import bundles, security
from ..deploy.db import session
from ..deploy.models import App, AppRevision, Device, DeploymentTarget
from ..deploy.schemas import COMPOSE_FILE_CANDIDATES
from .metrics import ToolContext


# ---------- helpers ----------


def _device_dict(d: Device) -> dict:
    return {
        "name": d.name,
        "id": str(d.id),
        "status": d.status,
        "last_seen": d.last_seen.isoformat() if d.last_seen else None,
        "agent_version": d.agent_version,
        "created_at": d.created_at.isoformat(),
    }


def _app_dict(a: App) -> dict:
    return {"name": a.name, "id": str(a.id), "created_at": a.created_at.isoformat()}


def _rev_dict(r: AppRevision) -> dict:
    return {
        "id": str(r.id),
        "version": r.version,
        "files": list(r.files.keys()) if r.files else [],
        "bundle_sha256": r.bundle_sha256,
        "created_at": r.created_at.isoformat(),
    }


async def _app_by_name(s, name: str) -> App | None:
    row = await s.execute(select(App).where(App.name == name))
    return row.scalar_one_or_none()


async def _device_by_name(s, name: str) -> Device | None:
    row = await s.execute(select(Device).where(Device.name == name))
    return row.scalar_one_or_none()


def _ensure_deploy_enabled() -> dict | None:
    if not settings.deploy_enabled:
        return {"error": "Drift Deploy is not configured (DRIFT_PG_URL / B2_BUCKET unset)"}
    return None


# ---------- Discovery ----------


async def list_devices(_ctx: ToolContext, _args: dict) -> dict:
    if (err := _ensure_deploy_enabled()):
        return err
    async with session() as s:
        rows = await s.execute(select(Device).order_by(Device.created_at.desc()))
        devices = [_device_dict(d) for d in rows.scalars().all()]
    return {"n": len(devices), "devices": devices}


async def get_device(_ctx: ToolContext, args: dict) -> dict:
    if (err := _ensure_deploy_enabled()):
        return err
    name = args.get("name")
    if not name:
        return {"error": "name is required"}
    async with session() as s:
        device = await _device_by_name(s, name)
        if device is None:
            return {"error": f"device '{name}' not found"}
        # Deployments on this device.
        rows = await s.execute(
            select(DeploymentTarget, App, AppRevision)
            .join(App, App.id == DeploymentTarget.app_id)
            .join(AppRevision, AppRevision.id == DeploymentTarget.desired_revision_id, isouter=True)
            .where(DeploymentTarget.device_id == device.id)
        )
        deployments = []
        for target, app, rev in rows.all():
            deployments.append({
                "app": app.name,
                "status": target.status,
                "desired_version": rev.version if rev else None,
                "current_revision_id": str(target.current_revision_id) if target.current_revision_id else None,
                "attempts": target.attempts,
                "last_error": target.last_error,
                "updated_at": target.updated_at.isoformat(),
            })
    return {"device": _device_dict(device), "deployments": deployments}


async def list_apps(_ctx: ToolContext, _args: dict) -> dict:
    if (err := _ensure_deploy_enabled()):
        return err
    async with session() as s:
        rows = await s.execute(
            select(App, func.count(AppRevision.id))
            .join(AppRevision, AppRevision.app_id == App.id, isouter=True)
            .group_by(App.id)
            .order_by(App.created_at.desc())
        )
        apps = []
        for app, n_revs in rows.all():
            apps.append({**_app_dict(app), "revisions": int(n_revs)})
    return {"n": len(apps), "apps": apps}


async def list_app_revisions(_ctx: ToolContext, args: dict) -> dict:
    if (err := _ensure_deploy_enabled()):
        return err
    name = args.get("app")
    if not name:
        return {"error": "app is required"}
    async with session() as s:
        app = await _app_by_name(s, name)
        if app is None:
            return {"error": f"app '{name}' not found"}
        rows = await s.execute(
            select(AppRevision).where(AppRevision.app_id == app.id).order_by(AppRevision.version.desc())
        )
        revs = [_rev_dict(r) for r in rows.scalars().all()]
    return {"app": name, "n": len(revs), "revisions": revs}


async def list_deployments(_ctx: ToolContext, _args: dict) -> dict:
    if (err := _ensure_deploy_enabled()):
        return err
    async with session() as s:
        rows = await s.execute(
            select(DeploymentTarget, Device, App, AppRevision)
            .join(Device, Device.id == DeploymentTarget.device_id)
            .join(App, App.id == DeploymentTarget.app_id)
            .join(AppRevision, AppRevision.id == DeploymentTarget.desired_revision_id, isouter=True)
            .order_by(DeploymentTarget.updated_at.desc())
        )
        out = []
        for target, device, app, desired_rev in rows.all():
            out.append({
                "device": device.name,
                "app": app.name,
                "desired_version": desired_rev.version if desired_rev else None,
                "current_revision_id": str(target.current_revision_id) if target.current_revision_id else None,
                "status": target.status,
                "attempts": target.attempts,
                "last_error": target.last_error,
                "updated_at": target.updated_at.isoformat(),
            })
    return {"n": len(out), "deployments": out}


# ---------- Lifecycle / commissioning ----------


async def commission_device(_ctx: ToolContext, args: dict) -> dict:
    if (err := _ensure_deploy_enabled()):
        return err
    name = args.get("name")
    if not name:
        return {"error": "name is required"}
    async with session() as s:
        if await _device_by_name(s, name):
            return {"error": f"device '{name}' already exists; delete it first or pick a new name"}
        token = security.generate_bootstrap_token()
        device = Device(name=name, bootstrap_token_hash=security.hash_token(token))
        s.add(device)
        await s.commit()
        await s.refresh(device)
    install_cmd = _render_install_cmd(name, token)
    return {
        "device": _device_dict(device),
        "bootstrap_token": token,
        "install_cmd": install_cmd,
        "guidance": (
            "Paste the install_cmd on the new device as root. The bootstrap_token shown "
            "here is the only credential the agent needs and is shown ONCE — treat it like "
            "a password. Fill in MANAGED_APPS= with a comma-separated list of apps this "
            "device is allowed to deploy (e.g. MANAGED_APPS=podnot,reporter)."
        ),
    }


async def delete_device(_ctx: ToolContext, args: dict) -> dict:
    if (err := _ensure_deploy_enabled()):
        return err
    name = args.get("name")
    if not name:
        return {"error": "name is required"}
    async with session() as s:
        device = await _device_by_name(s, name)
        if device is None:
            return {"error": f"device '{name}' not found"}
        await s.delete(device)
        await s.commit()
    return {"deleted": name}


def _render_install_cmd(name: str, token: str) -> str:
    # Mirror of routes_admin._render_install_cmd. Bearer-only auth — Caddy
    # is configured to NOT basic_auth /drift/api/deploy/agent/* paths.
    return (
        f"curl -sSL https://drift.example.com/drift/api/deploy/agent/install.sh | "
        f"DEVICE_NAME={name} BOOTSTRAP_TOKEN={token} "
        f"CP_URL=https://drift.example.com/drift/api/deploy "
        f"MANAGED_APPS= sudo -E bash"
    )


# ---------- App + revision ----------


async def create_app(_ctx: ToolContext, args: dict) -> dict:
    if (err := _ensure_deploy_enabled()):
        return err
    name = args.get("name")
    if not name:
        return {"error": "name is required"}
    async with session() as s:
        if await _app_by_name(s, name):
            return {"error": f"app '{name}' already exists"}
        app = App(name=name)
        s.add(app)
        await s.commit()
        await s.refresh(app)
    return {"app": _app_dict(app), "guidance": "Now author the first revision with apply_app_revision."}


def _validate_files(files: dict[str, str]) -> str | None:
    if not files:
        return "files is empty"
    for fname in files:
        if "/" in fname or (fname.startswith(".") and fname != ".env"):
            return f"file '{fname}': only basenames are allowed; no paths or leading dots (except .env)"
    if not any(name in files for name in COMPOSE_FILE_CANDIDATES):
        return f"bundle must contain one of: {', '.join(COMPOSE_FILE_CANDIDATES)}"
    return None


async def propose_app_revision(_ctx: ToolContext, args: dict) -> dict:
    """Pure preview of what apply_app_revision would do. No side effect."""
    if (err := _ensure_deploy_enabled()):
        return err
    name = args.get("app")
    files = args.get("files") or {}
    if not name:
        return {"error": "app is required"}
    err = _validate_files(files)
    if err:
        return {"error": err}
    async with session() as s:
        app = await _app_by_name(s, name)
        if app is None:
            return {"error": f"app '{name}' not found; call create_app first"}
        latest = await s.execute(
            select(AppRevision.version)
            .where(AppRevision.app_id == app.id)
            .order_by(AppRevision.version.desc())
            .limit(1)
        )
        last = latest.scalar_one_or_none()
    next_version = (last or 0) + 1
    # Pack to compute the would-be sha256 even though we don't upload.
    data, digest = bundles.pack(files)
    return {
        "action": "create_revision",
        "app": name,
        "next_version": next_version,
        "files": sorted(files.keys()),
        "bundle_bytes": len(data),
        "bundle_sha256": digest,
    }


async def apply_app_revision(_ctx: ToolContext, args: dict) -> dict:
    """Pack + upload the bundle, persist the revision."""
    if (err := _ensure_deploy_enabled()):
        return err
    name = args.get("app")
    files = args.get("files") or {}
    if not name:
        return {"error": "app is required"}
    err = _validate_files(files)
    if err:
        return {"error": err}
    async with session() as s:
        app = await _app_by_name(s, name)
        if app is None:
            return {"error": f"app '{name}' not found; call create_app first"}
        latest = await s.execute(
            select(AppRevision.version)
            .where(AppRevision.app_id == app.id)
            .order_by(AppRevision.version.desc())
            .limit(1)
        )
        next_version = (latest.scalar_one_or_none() or 0) + 1

        try:
            data, digest = bundles.pack(files)
            bundle_url = bundles.upload_bundle(app.name, next_version, data)
        except Exception as e:  # noqa: BLE001
            return {"error": f"bundle pack/upload failed: {e}"}

        rev = AppRevision(
            app_id=app.id,
            version=next_version,
            files=files,
            bundle_url=bundle_url,
            bundle_sha256=digest,
        )
        s.add(rev)
        await s.commit()
        await s.refresh(rev)
    return {
        "app": name,
        "version": next_version,
        "revision_id": str(rev.id),
        "bundle_sha256": digest,
    }


# ---------- Deployment ----------


async def deploy_revision(_ctx: ToolContext, args: dict) -> dict:
    """Set desired state: device should run a specific revision (default: latest)."""
    if (err := _ensure_deploy_enabled()):
        return err
    app_name = args.get("app")
    device_name = args.get("device")
    if not app_name or not device_name:
        return {"error": "app and device are required"}
    revision_id_str = args.get("revision_id")

    async with session() as s:
        app = await _app_by_name(s, app_name)
        if app is None:
            return {"error": f"app '{app_name}' not found"}
        device = await _device_by_name(s, device_name)
        if device is None:
            return {"error": f"device '{device_name}' not found"}

        rev: AppRevision | None
        if revision_id_str:
            try:
                rev_id = uuid.UUID(revision_id_str)
            except ValueError:
                return {"error": f"revision_id is not a valid uuid: {revision_id_str}"}
            rev = (await s.execute(
                select(AppRevision).where(AppRevision.id == rev_id, AppRevision.app_id == app.id)
            )).scalar_one_or_none()
            if rev is None:
                return {"error": "revision_id does not belong to that app"}
        else:
            rev = (await s.execute(
                select(AppRevision).where(AppRevision.app_id == app.id).order_by(AppRevision.version.desc()).limit(1)
            )).scalar_one_or_none()
            if rev is None:
                return {"error": f"app '{app_name}' has no revisions yet — call apply_app_revision first"}

        existing = (await s.execute(
            select(DeploymentTarget).where(
                DeploymentTarget.device_id == device.id,
                DeploymentTarget.app_id == app.id,
            )
        )).scalar_one_or_none()
        if existing is None:
            existing = DeploymentTarget(
                device_id=device.id,
                app_id=app.id,
                desired_revision_id=rev.id,
                status="pending",
            )
            s.add(existing)
            action = "created"
        else:
            existing.desired_revision_id = rev.id
            if existing.current_revision_id != rev.id:
                existing.status = "pending"
                existing.last_error = None
            action = "updated"
        await s.commit()
        await s.refresh(existing)

    return {
        "action": action,
        "device": device_name,
        "app": app_name,
        "desired_version": rev.version,
        "status": existing.status,
        "note": "The device's edge agent will pick this up on the next check-in (≤30s).",
    }


# ---------- Schemas + handler registry ----------


DEPLOY_TOOLS: list[dict] = [
    {
        "name": "list_devices",
        "description": "List devices known to Drift Deploy with status (pending/online/offline), last_seen, and agent version.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_device",
        "description": "Detailed view of one device including all its deployment targets.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Device name."}},
            "required": ["name"],
        },
    },
    {
        "name": "commission_device",
        "description": (
            "Register a new device and return the one-time bootstrap token + a curl|sh "
            "install command the operator pastes onto the device as root. The token is "
            "shown ONCE — treat it like a password."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Stable, lowercase, hyphenated device name."}},
            "required": ["name"],
        },
    },
    {
        "name": "delete_device",
        "description": "Remove a device record (does NOT uninstall the edge agent on the device).",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "list_apps",
        "description": "List apps managed by Drift Deploy with their current revision counts.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_app_revisions",
        "description": "List revisions for one app (newest first), with bundle digest + filename listing.",
        "input_schema": {
            "type": "object",
            "properties": {"app": {"type": "string", "description": "App name."}},
            "required": ["app"],
        },
    },
    {
        "name": "list_deployments",
        "description": "List every (device, app) deployment target: desired version, current status, last error.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_app",
        "description": (
            "Create an app shell. After this, call apply_app_revision (or propose first) "
            "to attach a compose bundle."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "propose_app_revision",
        "description": (
            "Preview the next revision of an app — packs the bundle in memory to compute "
            "size + sha256 but does NOT upload or store anything. ALWAYS use this BEFORE "
            "apply_app_revision and show the user the proposed file list + version + size "
            "in a make_markdown block for confirmation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app": {"type": "string"},
                "files": {
                    "type": "object",
                    "description": (
                        "filename → contents. Must include a compose.yaml (or compose.yml / "
                        "docker-compose.yml). May include .env and any files referenced by "
                        "RELATIVE paths from the compose file (e.g. prometheus.yml, vector.yaml). "
                        "Absolute host paths in volumes (e.g. /var/run/docker.sock) stay as-is."
                    ),
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["app", "files"],
        },
    },
    {
        "name": "apply_app_revision",
        "description": (
            "Pack the bundle, upload to object storage, and create a new revision row. "
            "Idempotent against the version counter — every call increments. Use AFTER "
            "propose_app_revision + user confirmation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app": {"type": "string"},
                "files": {"type": "object", "additionalProperties": {"type": "string"}},
            },
            "required": ["app", "files"],
        },
    },
    {
        "name": "deploy_revision",
        "description": (
            "Set desired state: device should run a specific revision of an app. If "
            "revision_id is omitted, the latest revision is used. The device's edge agent "
            "applies it on the next check-in (≤30s)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app": {"type": "string"},
                "device": {"type": "string"},
                "revision_id": {
                    "type": "string",
                    "description": "Optional uuid; defaults to the latest revision.",
                },
            },
            "required": ["app", "device"],
        },
    },
]


DEPLOY_HANDLERS = {
    "list_devices": list_devices,
    "get_device": get_device,
    "commission_device": commission_device,
    "delete_device": delete_device,
    "list_apps": list_apps,
    "list_app_revisions": list_app_revisions,
    "list_deployments": list_deployments,
    "create_app": create_app,
    "propose_app_revision": propose_app_revision,
    "apply_app_revision": apply_app_revision,
    "deploy_revision": deploy_revision,
}
