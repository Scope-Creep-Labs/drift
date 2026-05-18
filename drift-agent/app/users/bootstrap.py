"""Ensure an admin user exists on startup.

Idempotent: creates the user if missing, or updates the password if it
differs from the env-supplied one. The admin always has role='admin'
and bypasses group membership checks (`has_all_groups` semantics live
in UserContext, not the DB).

Failure mode: if env vars are unset AND no admin row exists in the DB,
log a clear warning. Drift still serves traffic, but the only
self-recovery is to set the env vars and restart.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from ..config import settings
from ..deploy.db import session as db_session
from ..deploy.models import User
from .passwords import hash_password, verify_password


log = logging.getLogger(__name__)


async def ensure_bootstrap_admin() -> None:
    username = settings.drift_admin_username
    password = settings.drift_admin_password

    async with db_session() as s:
        existing_admin = (
            await s.execute(select(User).where(User.role == "admin"))
        ).scalars().first()

        if not username or not password:
            if existing_admin is None:
                log.warning(
                    "no admin users in DB and DRIFT_ADMIN_USERNAME/DRIFT_ADMIN_PASSWORD "
                    "are unset — system has no way to log in. Set these env vars "
                    "and restart, or manually INSERT an admin row."
                )
            else:
                log.info(
                    "DRIFT_ADMIN_USERNAME unset; leaving existing admin '%s' alone",
                    existing_admin.username,
                )
            return

        target = (
            await s.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()

        if target is None:
            log.info("bootstrap: creating admin user '%s'", username)
            target = User(
                username=username,
                password_hash=hash_password(password),
                role="admin",
            )
            s.add(target)
        else:
            # Idempotent updates. Promote to admin if somehow demoted;
            # update the password if env says something different.
            changed = False
            if target.role != "admin":
                log.info("bootstrap: promoting '%s' back to admin", username)
                target.role = "admin"
                changed = True
            if not verify_password(password, target.password_hash):
                log.info("bootstrap: updating password for '%s'", username)
                target.password_hash = hash_password(password)
                changed = True
            if not changed:
                log.debug("bootstrap: admin '%s' unchanged", username)
        await s.commit()
