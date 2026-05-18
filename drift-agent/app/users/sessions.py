"""Server-side session management.

Cookie value = an opaque uuid that maps to a row in `sessions`. We store
sessions server-side rather than using JWTs because:
  - revocation is just `DELETE FROM sessions WHERE id = ?`
  - rolling expiry doesn't require token rotation
  - the failure mode of a stolen cookie is bounded by expires_at
The trade-off — one DB roundtrip per authenticated request — is fine
at our scale.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..deploy.models import Session, User


# 30 days. Bumped on every authenticated request so active users don't
# get logged out; idle sessions age out.
SESSION_TTL = timedelta(days=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def create_session(db: AsyncSession, user: User) -> Session:
    """Mint a new session for a user. Caller commits."""
    row = Session(
        user_id=user.id,
        expires_at=_now() + SESSION_TTL,
    )
    db.add(row)
    await db.flush()
    return row


async def get_session(db: AsyncSession, session_id: uuid.UUID) -> Session | None:
    """Return the session row if it's still valid, None otherwise.

    Side effect on valid sessions: bumps expires_at by SESSION_TTL.
    Caller commits.
    """
    row = (
        await db.execute(select(Session).where(Session.id == session_id))
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.expires_at <= _now():
        # Lazy GC: nuke the expired row when we touch it.
        await db.delete(row)
        return None
    row.expires_at = _now() + SESSION_TTL
    return row


async def revoke_session(db: AsyncSession, session_id: uuid.UUID) -> None:
    """Tear down a session (logout). Idempotent."""
    row = (
        await db.execute(select(Session).where(Session.id == session_id))
    ).scalar_one_or_none()
    if row is not None:
        await db.delete(row)
