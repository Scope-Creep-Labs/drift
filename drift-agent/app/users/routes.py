"""HTTP routes for authentication + user management.

Mounted at /api/auth (login/logout/me) and /api/auth/users (admin CRUD).
The same response/request shapes back the SPA's login flow and the
admin-managed user list.
"""
from __future__ import annotations

import uuid
from typing import Annotated, AsyncIterator, Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..deploy.models import User, UserGroup
from .deps import (
    ROLE_ORDER,
    SESSION_COOKIE,
    UserContext,
    get_current_user,
    get_db,
    require_role,
)
from .passwords import hash_password, verify_password
from .sessions import create_session, revoke_session


router = APIRouter(prefix="/api/auth", tags=["auth"])


# ---------- Schemas ----------


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class UserOut(BaseModel):
    id: uuid.UUID
    username: str
    role: str
    groups: list[str]


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    role: str = Field(pattern="^(observe|deploy|admin)$")
    groups: list[str] = Field(default_factory=list)


class UserUpdate(BaseModel):
    # All optional: only fields the caller supplies are changed.
    password: Optional[str] = Field(default=None, min_length=8, max_length=256)
    role: Optional[str] = Field(default=None, pattern="^(observe|deploy|admin)$")
    groups: Optional[list[str]] = None


def _user_out(user: User, groups: list[str]) -> UserOut:
    return UserOut(id=user.id, username=user.username, role=user.role, groups=sorted(groups))


async def _groups_for(db: AsyncSession, user_id: uuid.UUID) -> list[str]:
    rows = (
        await db.execute(select(UserGroup.group_id).where(UserGroup.user_id == user_id))
    ).scalars().all()
    return list(rows)


# ---------- Login / logout / me ----------


@router.post("/login", response_model=UserOut)
async def login(
    body: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> UserOut:
    user = (
        await db.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    # Constant-ish work whether the user exists or not, to make timing
    # oracles slightly less informative. (bcrypt's verify dominates.)
    if user is None:
        # Burn a bcrypt verify on a dummy hash to keep timing similar.
        verify_password(body.password, "$2b$12$" + "x" * 53)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    sess = await create_session(db, user)
    user.last_login_at = sess.created_at
    groups = await _groups_for(db, user.id)
    await db.commit()

    # Cookie attributes:
    #   - HttpOnly: SPA never reads this; only the backend uses it.
    #   - SameSite=Lax: SPA + API are same-origin, so Lax suffices.
    #   - Secure: in production (HTTPS). Skipped in dev to keep
    #     localhost flows working without TLS.
    response.set_cookie(
        key=SESSION_COOKIE,
        value=str(sess.id),
        httponly=True,
        samesite="lax",
        secure=not settings.dev_mode,
        max_age=30 * 24 * 3600,
        path="/",
    )
    return _user_out(user, groups)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    drift_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    db: AsyncSession = Depends(get_db),
) -> None:
    if drift_session:
        try:
            sid = uuid.UUID(drift_session)
        except ValueError:
            sid = None
        if sid is not None:
            await revoke_session(db, sid)
            await db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me", response_model=UserOut)
async def me(user: UserContext = Depends(get_current_user)) -> UserOut:
    return UserOut(
        id=user.id,
        username=user.username,
        role=user.role,
        groups=sorted(user.groups),
    )


@router.post("/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: PasswordChangeRequest,
    actor: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Self-serve password change. Verifies the caller's current password,
    then updates the hash. Existing sessions stay valid — the caller is
    still authenticated under their current cookie."""
    if body.new_password == body.current_password:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "new password must differ from current"
        )
    user = (
        await db.execute(select(User).where(User.id == actor.id))
    ).scalar_one_or_none()
    if user is None:
        # Should be impossible — the dep guard already loaded the user.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "current password is incorrect"
        )
    user.password_hash = hash_password(body.new_password)
    await db.commit()


# ---------- Admin: user CRUD ----------


@router.get("/users", response_model=list[UserOut])
async def list_users(
    _admin: UserContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> list[UserOut]:
    rows = (await db.execute(select(User).order_by(User.username))).scalars().all()
    out: list[UserOut] = []
    for u in rows:
        groups = await _groups_for(db, u.id)
        out.append(_user_out(u, groups))
    return out


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate,
    _admin: UserContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> UserOut:
    existing = (
        await db.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"user '{body.username}' already exists")
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        role=body.role,
    )
    db.add(user)
    await db.flush()
    for g in body.groups:
        db.add(UserGroup(user_id=user.id, group_id=g))
    await db.commit()
    await db.refresh(user)
    return _user_out(user, body.groups)


@router.patch("/users/{username}", response_model=UserOut)
async def update_user(
    username: str,
    body: UserUpdate,
    actor: UserContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> UserOut:
    user = (
        await db.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"user '{username}' not found")
    if body.password is not None:
        user.password_hash = hash_password(body.password)
    if body.role is not None:
        # An admin demoting themselves is allowed (chain-of-command goes
        # to whatever other admins exist). Demoting the last admin would
        # lock the system out — guard against it.
        if user.id == actor.id and body.role != "admin":
            other_admins = (
                await db.execute(
                    select(User).where(User.role == "admin", User.id != actor.id)
                )
            ).scalars().first()
            if other_admins is None:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "cannot demote the last admin — create another admin first",
                )
        user.role = body.role
    if body.groups is not None:
        # Reset + re-insert. Composite PK on (user_id, group_id) so this
        # is the cleanest path; small N (handful per user).
        await db.execute(
            UserGroup.__table__.delete().where(UserGroup.user_id == user.id)
        )
        for g in body.groups:
            db.add(UserGroup(user_id=user.id, group_id=g))
    await db.commit()
    await db.refresh(user)
    groups = await _groups_for(db, user.id)
    return _user_out(user, groups)


@router.delete("/users/{username}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    username: str,
    actor: UserContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> None:
    user = (
        await db.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"user '{username}' not found")
    if user.id == actor.id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "cannot delete your own account — ask another admin",
        )
    if user.role == "admin":
        other_admins = (
            await db.execute(
                select(User).where(User.role == "admin", User.id != user.id)
            )
        ).scalars().first()
        if other_admins is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "cannot delete the last admin",
            )
    # cascade on user_groups, sessions via FK ondelete=CASCADE
    await db.delete(user)
    await db.commit()
