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
        async with session() as s:
            existing = (await s.execute(select(App).where(App.name == name))).scalar_one_or_none()
            if existing is not None:
                log.info("default-apps: %s already exists, skipping", name)
                continue
            app = App(name=name)
            s.add(app)
            await s.flush()
            try:
                data, digest = bundles.pack(files)
                bundle_url = bundles.upload_bundle(app.name, 1, data)
            except Exception:  # noqa: BLE001
                log.exception("default-apps: failed to pack/upload %s", name)
                await s.rollback()
                continue
            rev = AppRevision(
                app_id=app.id,
                version=1,
                files=files,
                bundle_url=bundle_url,
                bundle_sha256=digest,
            )
            s.add(rev)
            await s.commit()
            log.info("default-apps: seeded %s v1 (sha256=%s)", name, digest[:12])


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
