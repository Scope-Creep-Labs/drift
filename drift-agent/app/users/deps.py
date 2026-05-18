"""FastAPI dependencies that resolve the current user from a session cookie.

The contract:
  - `get_current_user` returns the user or raises 401.
  - `require_role(min_role)` is a dependency factory returning a guard
    that also checks the user's role.
  - `get_allowed_groups` returns the set of device groups this user can
    act on. Admin → all groups (sentinel: empty allow-list means "no
    restriction" at the call site, but we use a different shape — see
    `UserContext.is_admin`).

Roles are totally ordered: observe < deploy < admin. require_role checks
the user's role is >= the minimum.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated, AsyncIterator

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..deploy.db import session as db_session
from ..deploy.models import User, UserGroup
from .sessions import get_session


SESSION_COOKIE = "drift_session"

ROLE_ORDER = {"observe": 0, "deploy": 1, "admin": 2}


@dataclass(frozen=True)
class UserContext:
    """Read-only snapshot of the requesting user. Built once per request."""

    id: uuid.UUID
    username: str
    role: str
    # Empty for non-admin users with no group memberships; admins ignore
    # this entirely (they bypass group checks).
    groups: frozenset[str]

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_deploy(self) -> bool:
        # admin ⊃ deploy
        return ROLE_ORDER.get(self.role, -1) >= ROLE_ORDER["deploy"]

    def has_group(self, group_id: str) -> bool:
        """Can this user act on devices in this group?"""
        if self.is_admin:
            return True
        return group_id in self.groups

    def role_at_least(self, min_role: str) -> bool:
        return ROLE_ORDER.get(self.role, -1) >= ROLE_ORDER[min_role]


async def get_db() -> AsyncIterator[AsyncSession]:
    async with db_session() as s:
        yield s


async def get_current_user(
    drift_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    db: AsyncSession = Depends(get_db),
) -> UserContext:
    if not drift_session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not signed in")
    try:
        sid = uuid.UUID(drift_session)
    except ValueError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid session cookie")
    sess = await get_session(db, sid)
    if sess is None:
        await db.commit()  # commit the lazy GC of expired session if any
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session expired")
    user = (
        await db.execute(select(User).where(User.id == sess.user_id))
    ).scalar_one_or_none()
    if user is None:
        # Session row outlived its user — clean up.
        await db.delete(sess)
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user no longer exists")
    groups = {
        g.group_id
        for g in (
            await db.execute(select(UserGroup).where(UserGroup.user_id == user.id))
        ).scalars().all()
    }
    # Commit the bumped expires_at from get_session.
    await db.commit()
    return UserContext(
        id=user.id,
        username=user.username,
        role=user.role,
        groups=frozenset(groups),
    )


def require_role(min_role: str):
    """Dependency factory: only proceed if the current user has at least
    `min_role`. Returns the same UserContext so endpoints can keep using
    it without re-resolving."""
    if min_role not in ROLE_ORDER:
        raise ValueError(f"unknown role: {min_role}")

    async def _guard(user: UserContext = Depends(get_current_user)) -> UserContext:
        if not user.role_at_least(min_role):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"requires role >= {min_role}; you have {user.role}",
            )
        return user

    return _guard
