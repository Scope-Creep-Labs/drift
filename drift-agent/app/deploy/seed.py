"""Default-apps seeder.

Each directory under ``/opt/drift-defaults/apps/`` (baked into the
image at build time from ``drift-agent/default-apps/``) becomes an
``App`` row + initial ``AppRevision`` the first time the CP boots
against an empty database.

Re-running is idempotent — if the app already exists, we leave it
alone (and any subsequent edits go through the normal
propose/apply_revision tools so the operator's history is preserved).
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select

from .db import session
from . import bundles
from .models import App, AppRevision

log = logging.getLogger(__name__)

DEFAULTS_DIR = Path("/opt/drift-defaults/apps")


async def seed_default_apps() -> None:
    """Idempotent: create one App + initial revision per directory under
    DEFAULTS_DIR. Skips silently if the directory is missing (dev runs
    out of the source tree don't have the COPY baked in)."""
    if not DEFAULTS_DIR.is_dir():
        log.info("default-apps: %s missing, skipping seed", DEFAULTS_DIR)
        return

    for app_dir in sorted(DEFAULTS_DIR.iterdir()):
        if not app_dir.is_dir():
            continue
        name = app_dir.name
        files = _read_bundle_files(app_dir)
        if not files:
            log.warning("default-apps: %s has no files, skipping", name)
            continue
        # Compute the bundle SHA up front; we compare against the latest
        # revision's hash to decide whether this is a no-op or a new rev.
        try:
            data, digest = bundles.pack(files)
        except Exception:  # noqa: BLE001
            log.exception("default-apps: pack failed for %s", name)
            continue

        async with session() as s:
            app = (await s.execute(select(App).where(App.name == name))).scalar_one_or_none()
            if app is None:
                app = App(name=name)
                s.add(app)
                await s.flush()
                next_version = 1
            else:
                latest = await s.execute(
                    select(AppRevision)
                    .where(AppRevision.app_id == app.id)
                    .order_by(AppRevision.version.desc())
                    .limit(1)
                )
                latest_rev = latest.scalar_one_or_none()
                if latest_rev is not None and latest_rev.bundle_sha256 == digest:
                    log.info("default-apps: %s up-to-date (sha=%s)", name, digest[:12])
                    continue
                next_version = (latest_rev.version + 1) if latest_rev is not None else 1

            try:
                bundle_url = bundles.upload_bundle(app.name, next_version, data)
            except Exception:  # noqa: BLE001
                log.exception("default-apps: upload failed for %s v%d", name, next_version)
                await s.rollback()
                continue
            rev = AppRevision(
                app_id=app.id,
                version=next_version,
                files=files,
                bundle_url=bundle_url,
                bundle_sha256=digest,
            )
            s.add(rev)
            await s.commit()
            log.info("default-apps: seeded %s v%d (sha=%s)", name, next_version, digest[:12])


def _read_bundle_files(app_dir: Path) -> dict[str, str]:
    """Read every file (recursive) and return as {relative_path: text}.
    Skips dotfiles + anything non-utf8. Matches the shape that
    bundles.pack() expects."""
    out: dict[str, str] = {}
    for path in sorted(app_dir.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        try:
            out[str(path.relative_to(app_dir))] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            log.warning("default-apps: %s skipped (non-utf8)", path)
    return out
