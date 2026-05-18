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
        "group_id": d.group_id,
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


def _require_deploy_role(ctx: ToolContext) -> dict | None:
    """Defense-in-depth: even though the /investigate HTTP endpoint
    enforces auth, the tools call into the DB directly and could be
    invoked by an observe-role user through the LLM. Returning an error
    here means the LLM sees a permission-denied response, which it can
    relay back to the operator cleanly."""
    user = getattr(ctx, "user", None)
    if user is None:
        # Test/dev context — allow.
        return None
    if not user.is_deploy:
        return {
            "error": (
                f"permission denied: operator '{user.username}' has role '{user.role}', "
                f"which cannot deploy. Required role: 'deploy' or 'admin'."
            )
        }
    return None


def _require_admin_role(ctx: ToolContext) -> dict | None:
    user = getattr(ctx, "user", None)
    if user is None:
        return None
    if not user.is_admin:
        return {
            "error": (
                f"permission denied: operator '{user.username}' has role '{user.role}', "
                "which can't manage admin-only settings (registry credentials, users)."
            )
        }
    return None


def _check_group_access(ctx: ToolContext, group_id: str | None) -> dict | None:
    """Return an error dict if the operator isn't allowed to act on
    devices in this group. Admins bypass; observe role doesn't reach
    this check (caller should _require_deploy_role first)."""
    user = getattr(ctx, "user", None)
    if user is None or user.is_admin:
        return None
    if group_id is None or not user.has_group(group_id):
        return {
            "error": (
                f"permission denied: operator '{user.username}' doesn't have access to "
                f"group '{group_id or '<none>'}' (allowed: {sorted(user.groups)})"
            )
        }
    return None


# ---------- Discovery ----------


async def list_devices(ctx: ToolContext, _args: dict) -> dict:
    if (err := _ensure_deploy_enabled()):
        return err
    user = getattr(ctx, "user", None)
    async with session() as s:
        q = select(Device).order_by(Device.created_at.desc())
        if user is not None and not user.is_admin:
            q = q.where(Device.group_id.in_(user.groups))
        rows = await s.execute(q)
        devices = [_device_dict(d) for d in rows.scalars().all()]
    return {"n": len(devices), "devices": devices}


async def get_device(ctx: ToolContext, args: dict) -> dict:
    if (err := _ensure_deploy_enabled()):
        return err
    name = args.get("name")
    if not name:
        return {"error": "name is required"}
    async with session() as s:
        device = await _device_by_name(s, name)
        if device is None:
            return {"error": f"device '{name}' not found"}
        if (err := _check_group_access(ctx, device.group_id)):
            return err
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
                "max_retries": target.max_retries,
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


async def get_app_revision(_ctx: ToolContext, args: dict) -> dict:
    """Return the full file contents of one app revision. The natural read
    side of propose/apply_app_revision — let the agent fetch v1, patch
    one file, ship as v2 without forcing the operator to paste everything
    back from scratch.
    """
    if (err := _ensure_deploy_enabled()):
        return err
    name = args.get("app")
    if not name:
        return {"error": "app is required"}
    version = args.get("version")
    revision_id_str = args.get("revision_id")

    async with session() as s:
        app = await _app_by_name(s, name)
        if app is None:
            return {"error": f"app '{name}' not found"}

        rev: AppRevision | None = None
        if revision_id_str:
            try:
                rev_id = uuid.UUID(revision_id_str)
            except ValueError:
                return {"error": f"invalid revision_id: {revision_id_str}"}
            rev = (await s.execute(
                select(AppRevision).where(AppRevision.id == rev_id, AppRevision.app_id == app.id)
            )).scalar_one_or_none()
        elif version is not None:
            try:
                version_int = int(version)
            except (TypeError, ValueError):
                return {"error": f"version must be an integer, got: {version!r}"}
            rev = (await s.execute(
                select(AppRevision).where(
                    AppRevision.app_id == app.id, AppRevision.version == version_int
                )
            )).scalar_one_or_none()
        else:
            rev = (await s.execute(
                select(AppRevision)
                .where(AppRevision.app_id == app.id)
                .order_by(AppRevision.version.desc())
                .limit(1)
            )).scalar_one_or_none()

        if rev is None:
            return {"error": "revision not found"}

    return {
        "app": name,
        "version": rev.version,
        "revision_id": str(rev.id),
        "files": rev.files,
        "bundle_sha256": rev.bundle_sha256,
        "created_at": rev.created_at.isoformat(),
    }


async def list_deployments(ctx: ToolContext, args: dict) -> dict:
    if (err := _ensure_deploy_enabled()):
        return err
    user = getattr(ctx, "user", None)
    include_removed = bool(args.get("include_removed", False))
    async with session() as s:
        query = (
            select(DeploymentTarget, Device, App, AppRevision)
            .join(Device, Device.id == DeploymentTarget.device_id)
            .join(App, App.id == DeploymentTarget.app_id)
            .join(AppRevision, AppRevision.id == DeploymentTarget.desired_revision_id, isouter=True)
            .order_by(DeploymentTarget.updated_at.desc())
        )
        if user is not None and not user.is_admin:
            # Filter to the user's groups by joining through Device.
            query = query.where(Device.group_id.in_(user.groups))
        if not include_removed:
            # `removed` is the tombstone status — hide from the active view
            # but keep the row for audit (re-enable via include_removed=true).
            query = query.where(DeploymentTarget.status != "removed")
        rows = await s.execute(query)
        out = []
        for target, device, app, desired_rev in rows.all():
            out.append({
                "device": device.name,
                "app": app.name,
                "desired_version": desired_rev.version if desired_rev else None,
                "current_revision_id": str(target.current_revision_id) if target.current_revision_id else None,
                "status": target.status,
                "attempts": target.attempts,
                "max_retries": target.max_retries,
                "last_error": target.last_error,
                "updated_at": target.updated_at.isoformat(),
            })
    return {"n": len(out), "deployments": out, "include_removed": include_removed}


# ---------- Lifecycle / commissioning ----------


async def commission_device(ctx: ToolContext, args: dict) -> dict:
    if (err := _require_deploy_role(ctx)):
        return err
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
            "Paste the install_cmd on the new device as root. Only host dep is Docker — "
            "the agent runs as a container, so no systemd, no jq, no docker compose "
            "plugin needed on the host. Works on Linux VMs, Raspberry Pi, Synology NAS, "
            "anywhere Docker runs. The bootstrap_token is shown ONCE — treat it like a "
            "password. **REPLACE GROUP_ID=** in the one-liner with a logical grouping "
            "for this device (e.g. cloud, edge, client-x, prod) — required, used as "
            "${DRIFT_GROUP_ID} in bundles. Protected service/container names "
            "(drift-agent, drift-postgres, drift-frontend, drift-deploy-agent) are "
            "refused by the agent as a bricking safeguard."
        ),
    }


async def delete_device(ctx: ToolContext, args: dict) -> dict:
    if (err := _require_deploy_role(ctx)):
        return err
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
        f"GROUP_ID=CHOOSE_ONE_OF=cloud|edge|client-x|prod sudo -E bash"
    )


# ---------- App + revision ----------


async def create_app(ctx: ToolContext, args: dict) -> dict:
    if (err := _require_deploy_role(ctx)):
        return err
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


async def propose_app_revision(ctx: ToolContext, args: dict) -> dict:
    if (err := _require_deploy_role(ctx)):
        return err
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


async def fork_app(ctx: ToolContext, args: dict) -> dict:
    if (err := _require_deploy_role(ctx)):
        return err
    """Copy an existing app's revision as a new app's first revision.

    Atomic create_app + apply_app_revision for the common "I want a
    parallel app like X for purpose Y" case (e.g. reporter →
    reporter-jetson). No propose dance — the bytes come verbatim from
    a stored revision; the user can verify via list_app_revisions on
    the target afterwards.

    Behavior:
    - If target_app doesn't exist, it's created.
    - If target_app exists, the copy lands as the next sequential
      revision on it. (Useful for "make target_app match source_app
      v_n" without authoring files manually.)
    """
    if (err := _ensure_deploy_enabled()):
        return err
    source = args.get("source_app")
    target = args.get("target_app")
    if not source or not target:
        return {"error": "source_app and target_app are required"}
    if source == target:
        return {"error": "source_app and target_app must differ; use apply_app_revision to add a version to the same app"}
    source_version_arg = args.get("source_version")

    async with session() as s:
        src = await _app_by_name(s, source)
        if src is None:
            return {"error": f"source app '{source}' not found"}

        if source_version_arg is not None:
            try:
                want = int(source_version_arg)
            except (TypeError, ValueError):
                return {"error": f"source_version must be an integer, got {source_version_arg!r}"}
            src_rev = (await s.execute(
                select(AppRevision).where(
                    AppRevision.app_id == src.id,
                    AppRevision.version == want,
                )
            )).scalar_one_or_none()
        else:
            src_rev = (await s.execute(
                select(AppRevision)
                .where(AppRevision.app_id == src.id)
                .order_by(AppRevision.version.desc())
                .limit(1)
            )).scalar_one_or_none()
        if src_rev is None:
            return {"error": f"source revision not found in '{source}'"}

        target_app_obj = await _app_by_name(s, target)
        target_created = False
        if target_app_obj is None:
            target_app_obj = App(name=target)
            s.add(target_app_obj)
            await s.flush()
            target_created = True

        latest = await s.execute(
            select(AppRevision.version)
            .where(AppRevision.app_id == target_app_obj.id)
            .order_by(AppRevision.version.desc())
            .limit(1)
        )
        next_version = (latest.scalar_one_or_none() or 0) + 1

        try:
            data, digest = bundles.pack(src_rev.files)
            bundle_url = bundles.upload_bundle(target_app_obj.name, next_version, data)
        except Exception as e:  # noqa: BLE001
            return {"error": f"bundle pack/upload failed: {e}"}

        new_rev = AppRevision(
            app_id=target_app_obj.id,
            version=next_version,
            files=src_rev.files,
            bundle_url=bundle_url,
            bundle_sha256=digest,
        )
        s.add(new_rev)
        await s.commit()
        await s.refresh(new_rev)

    return {
        "source_app": source,
        "source_version": src_rev.version,
        "source_revision_id": str(src_rev.id),
        "target_app": target,
        "target_app_created": target_created,
        "version": new_rev.version,
        "revision_id": str(new_rev.id),
        "bundle_sha256": digest,
        "files": list(src_rev.files.keys()),
    }


async def apply_app_revision(ctx: ToolContext, args: dict) -> dict:
    if (err := _require_deploy_role(ctx)):
        return err
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


async def deploy_revision(ctx: ToolContext, args: dict) -> dict:
    if (err := _require_deploy_role(ctx)):
        return err
    """Set desired state: device should run a specific revision (default: latest)."""
    if (err := _ensure_deploy_enabled()):
        return err
    app_name = args.get("app")
    device_name = args.get("device")
    if not app_name or not device_name:
        return {"error": "app and device are required"}
    revision_id_str = args.get("revision_id")
    max_retries_raw = args.get("max_retries")
    max_retries: int | None = None
    if max_retries_raw is not None:
        try:
            max_retries = int(max_retries_raw)
            if max_retries < 1 or max_retries > 100:
                return {"error": "max_retries must be between 1 and 100"}
        except (TypeError, ValueError):
            return {"error": f"max_retries must be an integer, got {max_retries_raw!r}"}

    async with session() as s:
        app = await _app_by_name(s, app_name)
        if app is None:
            return {"error": f"app '{app_name}' not found"}
        device = await _device_by_name(s, device_name)
        if device is None:
            return {"error": f"device '{device_name}' not found"}
        if (err := _check_group_access(ctx, device.group_id)):
            return err

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
                **({"max_retries": max_retries} if max_retries is not None else {}),
            )
            s.add(existing)
            action = "created"
        else:
            prior_revision = existing.desired_revision_id
            existing.desired_revision_id = rev.id
            if max_retries is not None:
                existing.max_retries = max_retries
            # Reset retry counter on revision change OR explicit resume
            # from paused_retries (this counts as an operator-initiated
            # retry attempt regardless of revision change).
            revision_changed = prior_revision != rev.id
            is_paused = existing.status == "paused_retries"
            if revision_changed or is_paused:
                existing.status = "pending"
                existing.last_error = None
                existing.attempts = 0
            action = "updated"
        await s.commit()
        await s.refresh(existing)

    return {
        "action": action,
        "device": device_name,
        "app": app_name,
        "desired_version": rev.version,
        "status": existing.status,
        "max_retries": existing.max_retries,
        "note": "The device's edge agent will pick this up on the next check-in (≤30s).",
    }


async def retry_deployment(ctx: ToolContext, args: dict) -> dict:
    if (err := _require_deploy_role(ctx)):
        return err
    """Resume a paused_retries deployment without changing the revision.

    When attempts hits max_retries the target is paused (CP stops shipping
    the bundle, agent stops retrying). retry_deployment clears attempts +
    last_error and flips status back to pending, so the next check-in
    re-issues the bundle. Use this after fixing the underlying problem
    (e.g. set registry credentials, fix a typo in the compose) — you
    don't need to bump the revision just to get one more attempt.
    """
    if (err := _ensure_deploy_enabled()):
        return err
    app_name = args.get("app")
    device_name = args.get("device")
    if not app_name or not device_name:
        return {"error": "app and device are required"}
    max_retries_raw = args.get("max_retries")
    new_cap: int | None = None
    if max_retries_raw is not None:
        try:
            new_cap = int(max_retries_raw)
            if new_cap < 1 or new_cap > 100:
                return {"error": "max_retries must be between 1 and 100"}
        except (TypeError, ValueError):
            return {"error": f"max_retries must be an integer, got {max_retries_raw!r}"}

    async with session() as s:
        app = await _app_by_name(s, app_name)
        if app is None:
            return {"error": f"app '{app_name}' not found"}
        device = await _device_by_name(s, device_name)
        if device is None:
            return {"error": f"device '{device_name}' not found"}

        target = (await s.execute(
            select(DeploymentTarget).where(
                DeploymentTarget.device_id == device.id,
                DeploymentTarget.app_id == app.id,
            )
        )).scalar_one_or_none()
        if target is None:
            return {"error": f"no deployment target for {device_name}/{app_name} — call deploy_revision first"}
        if target.desired_revision_id is None:
            return {
                "error": f"deployment is in removal lifecycle (status={target.status}); call deploy_revision to redeploy instead of retry",
            }

        prior_status = target.status
        prior_attempts = target.attempts
        target.status = "pending"
        target.attempts = 0
        target.last_error = None
        if new_cap is not None:
            target.max_retries = new_cap
        await s.commit()
        await s.refresh(target)

    return {
        "device": device_name,
        "app": app_name,
        "prior_status": prior_status,
        "prior_attempts": prior_attempts,
        "status": target.status,
        "attempts": target.attempts,
        "max_retries": target.max_retries,
        "note": "Edge agent will reattempt the deploy on its next check-in (≤30s).",
    }


async def delete_deployment(ctx: ToolContext, args: dict) -> dict:
    if (err := _require_deploy_role(ctx)):
        return err
    """Mark a deployment for removal on one device.

    Sets desired_revision_id = NULL on the target row + status = "removing".
    On the next check-in the agent runs `docker compose -p <app> down`
    and drops the app from its local state. Once the agent confirms the
    stop on its following check-in the row is tombstoned with
    status = "removed" — the row STAYS so we keep an audit trail of
    what ever ran where. Deploy the same (app, device) again to
    resurrect the row to "pending".
    """
    if (err := _ensure_deploy_enabled()):
        return err
    app_name = args.get("app")
    device_name = args.get("device")
    if not app_name or not device_name:
        return {"error": "app and device are required"}

    async with session() as s:
        app = await _app_by_name(s, app_name)
        if app is None:
            return {"error": f"app '{app_name}' not found"}
        device = await _device_by_name(s, device_name)
        if device is None:
            return {"error": f"device '{device_name}' not found"}

        target = (await s.execute(
            select(DeploymentTarget).where(
                DeploymentTarget.device_id == device.id,
                DeploymentTarget.app_id == app.id,
            )
        )).scalar_one_or_none()
        if target is None:
            return {"already_absent": True, "device": device_name, "app": app_name}

        target.desired_revision_id = None
        target.status = "removing"
        target.last_error = None
        await s.commit()

    return {
        "device": device_name,
        "app": app_name,
        "status": "removing",
        "note": (
            "The device's edge agent will run `docker compose down` for this app "
            "on the next check-in (≤30s), then the target row is deleted "
            "server-side. Use list_deployments to watch the transition."
        ),
    }


async def delete_deployment_from_group(ctx: ToolContext, args: dict) -> dict:
    if (err := _require_deploy_role(ctx)):
        return err
    """Mark a deployment for removal across every device in a group."""
    if (err := _ensure_deploy_enabled()):
        return err
    app_name = args.get("app")
    group = args.get("group_id")
    if not app_name or not group:
        return {"error": "app and group_id are required"}
    if (err := _check_group_access(ctx, group)):
        return err

    async with session() as s:
        app = await _app_by_name(s, app_name)
        if app is None:
            return {"error": f"app '{app_name}' not found"}

        devices = (await s.execute(
            select(Device).where(Device.group_id == group)
        )).scalars().all()
        if not devices:
            return {"error": f"no devices reporting group_id='{group}'"}

        results = []
        absent = []
        for device in devices:
            target = (await s.execute(
                select(DeploymentTarget).where(
                    DeploymentTarget.device_id == device.id,
                    DeploymentTarget.app_id == app.id,
                )
            )).scalar_one_or_none()
            if target is None:
                absent.append(device.name)
                continue
            target.desired_revision_id = None
            target.status = "removing"
            target.last_error = None
            results.append({"device": device.name, "status": "removing"})
        await s.commit()

    return {
        "app": app_name,
        "group_id": group,
        "marked_for_removal": results,
        "already_absent": absent,
        "note": "Each device's edge agent will compose down on next check-in (≤30s).",
    }


async def deploy_revision_to_group(ctx: ToolContext, args: dict) -> dict:
    if (err := _require_deploy_role(ctx)):
        return err
    """Deploy to every device in a logical group. Resolves group_id → devices,
    loops deploy_revision per device.

    Skips offline devices unless `include_offline=true`.
    """
    if (err := _ensure_deploy_enabled()):
        return err
    app_name = args.get("app")
    group = args.get("group_id")
    if not app_name or not group:
        return {"error": "app and group_id are required"}
    if (err := _check_group_access(ctx, group)):
        return err
    revision_id_str = args.get("revision_id")
    include_offline = bool(args.get("include_offline", False))
    max_retries_raw = args.get("max_retries")
    max_retries: int | None = None
    if max_retries_raw is not None:
        try:
            max_retries = int(max_retries_raw)
            if max_retries < 1 or max_retries > 100:
                return {"error": "max_retries must be between 1 and 100"}
        except (TypeError, ValueError):
            return {"error": f"max_retries must be an integer, got {max_retries_raw!r}"}

    async with session() as s:
        app = await _app_by_name(s, app_name)
        if app is None:
            return {"error": f"app '{app_name}' not found"}

        rows = await s.execute(select(Device).where(Device.group_id == group))
        devices = rows.scalars().all()
        if not devices:
            return {"error": f"no devices reporting group_id='{group}' (check 'list_devices')"}

        # Resolve revision once (latest unless explicit).
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
                return {"error": f"app '{app_name}' has no revisions yet"}

        results = []
        skipped = []
        for device in devices:
            if device.status != "online" and not include_offline:
                skipped.append({"device": device.name, "reason": f"status={device.status}"})
                continue

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
                    **({"max_retries": max_retries} if max_retries is not None else {}),
                )
                s.add(existing)
                action = "created"
            else:
                prior_revision = existing.desired_revision_id
                existing.desired_revision_id = rev.id
                if max_retries is not None:
                    existing.max_retries = max_retries
                revision_changed = prior_revision != rev.id
                is_paused = existing.status == "paused_retries"
                if revision_changed or is_paused:
                    existing.status = "pending"
                    existing.last_error = None
                    existing.attempts = 0
                action = "updated"
            results.append({"device": device.name, "action": action})
        await s.commit()

    return {
        "app": app_name,
        "group_id": group,
        "desired_version": rev.version,
        "deployed_to": results,
        "skipped": skipped,
        "note": "Each device's edge agent will pick this up on its next check-in (≤30s).",
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
        "name": "get_app_revision",
        "description": (
            "Return the FULL file contents of one app revision. Use this to read the "
            "current bundle BEFORE proposing a patch — e.g. fetch v1, change one line "
            "in compose.yaml, call propose_app_revision/apply_app_revision with the "
            "modified files. Selector: pass `version` (integer) or `revision_id` (uuid). "
            "If neither is given, returns the latest revision."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app": {"type": "string"},
                "version": {"type": "integer", "description": "Revision version number (1, 2, …)."},
                "revision_id": {"type": "string", "description": "Revision uuid."},
            },
            "required": ["app"],
        },
    },
    {
        "name": "list_deployments",
        "description": (
            "List every (device, app) deployment target: desired version, current status, "
            "last error. By default hides `removed` (tombstoned) deployments; pass "
            "`include_removed: true` to see the full audit log of what ever ran where."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "include_removed": {
                    "type": "boolean",
                    "description": "Also include status='removed' tombstone rows. Default false.",
                },
            },
        },
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
        "name": "fork_app",
        "description": (
            "Copy an existing app's revision as a new app's first revision. "
            "Atomic create_app + apply_app_revision for the 'I want a parallel "
            "app like X for purpose Y' case (e.g. reporter → reporter-jetson). "
            "No propose step needed — the bytes come verbatim from the source "
            "and the user can verify via list_app_revisions on the target. "
            "If target_app already exists, the copy lands as its next "
            "sequential revision. source_version defaults to latest."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_app": {"type": "string"},
                "target_app": {"type": "string"},
                "source_version": {
                    "type": "integer",
                    "description": "Source revision version. Defaults to latest.",
                },
            },
            "required": ["source_app", "target_app"],
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
            "applies it on the next check-in (≤30s). Optional max_retries overrides the "
            "per-target retry cap (default 5) — useful for finicky deploys that may need "
            "more attempts (large images, slow networks)."
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
                "max_retries": {
                    "type": "integer",
                    "description": "Cap on consecutive apply failures before the target is paused (1–100). Defaults to the existing value or 5 for new targets.",
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            "required": ["app", "device"],
        },
    },
    {
        "name": "retry_deployment",
        "description": (
            "Resume a paused_retries deployment. Resets attempts to 0 and flips status "
            "back to pending so the edge agent re-applies on its next check-in. Use this "
            "AFTER fixing the underlying cause (set credentials, fix compose typo, etc.) "
            "— there's no point retrying if the failure mode hasn't changed. Optional "
            "max_retries simultaneously bumps the cap for the resumed attempts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app": {"type": "string"},
                "device": {"type": "string"},
                "max_retries": {
                    "type": "integer",
                    "description": "Optional: also raise the retry cap to this value (1–100).",
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            "required": ["app", "device"],
        },
    },
    {
        "name": "delete_deployment",
        "description": (
            "Remove an app from one device. Marks the target for removal on the "
            "control plane; the edge agent will run `docker compose -p <app> down` "
            "on its next check-in and the row is deleted server-side once the agent "
            "confirms the stop. ALWAYS confirm with the user before calling — "
            "running services WILL be stopped."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app": {"type": "string"},
                "device": {"type": "string"},
            },
            "required": ["app", "device"],
        },
    },
    {
        "name": "delete_deployment_from_group",
        "description": (
            "Remove an app from every device in a logical group. Same lifecycle "
            "as delete_deployment but fans out. ALWAYS confirm with the user first "
            "AND list the devices that will be affected — group deletes are easy "
            "to fat-finger."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app": {"type": "string"},
                "group_id": {"type": "string"},
            },
            "required": ["app", "group_id"],
        },
    },
    {
        "name": "deploy_revision_to_group",
        "description": (
            "Deploy an app revision to every device in a logical group (group_id). "
            "Resolves the group to its member devices via the value each agent reports on "
            "check-in. Offline devices are skipped unless include_offline=true. Use this "
            "for 'deploy reporter to all home devices' style prompts. Optional max_retries "
            "applies to every target in the group."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app": {"type": "string"},
                "group_id": {
                    "type": "string",
                    "description": "Logical group reported by devices (e.g. cloud, edge, drift_home).",
                },
                "revision_id": {
                    "type": "string",
                    "description": "Optional uuid; defaults to the latest revision.",
                },
                "include_offline": {
                    "type": "boolean",
                    "description": "Include devices whose status != 'online'. Default false.",
                },
                "max_retries": {
                    "type": "integer",
                    "description": "Cap on consecutive apply failures before each target is paused (1–100).",
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            "required": ["app", "group_id"],
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
    "get_app_revision": get_app_revision,
    "list_deployments": list_deployments,
    "create_app": create_app,
    "propose_app_revision": propose_app_revision,
    "apply_app_revision": apply_app_revision,
    "fork_app": fork_app,
    "deploy_revision": deploy_revision,
    "deploy_revision_to_group": deploy_revision_to_group,
    "retry_deployment": retry_deployment,
    "delete_deployment": delete_deployment,
    "delete_deployment_from_group": delete_deployment_from_group,
}
