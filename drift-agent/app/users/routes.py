"""HTTP routes for authentication + user management.

Mounted at /api/auth (login/logout/me) and /api/auth/users (admin CRUD).
The same response/request shapes back the SPA's login flow and the
admin-managed user list.
"""
from __future__ import annotations

import uuid
from typing import Annotated, AsyncIterator, Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
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
from .rate_limit import client_ip_from_request, get_login_limiter
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
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> UserOut:
    # Rate-limit check BEFORE the database lookup + bcrypt verify, so a
    # lockout costs us almost nothing and an attacker can't keep
    # exhausting CPU on bcrypt past the threshold.
    limiter = get_login_limiter()
    user_key = f"user:{body.username.strip().lower()}"
    ip_key = f"ip:{client_ip_from_request(request)}"
    if await limiter.is_locked(user_key) or await limiter.is_locked(ip_key):
        # 429 instead of 401 so the SPA can surface a distinct
        # "too many attempts, try again later" message. We don't
        # leak whether the lockout is on the username or the IP
        # (that's an enumeration hint).
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many failed login attempts, try again later",
            headers={"Retry-After": str(settings.login_failure_window_seconds)},
        )

    user = (
        await db.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    # Constant-ish work whether the user exists or not, to make timing
    # oracles slightly less informative. (bcrypt's verify dominates.)
    if user is None:
        # Burn a bcrypt verify on a dummy hash to keep timing similar.
        verify_password(body.password, "$2b$12$" + "x" * 53)
        await limiter.record_failure(user_key)
        await limiter.record_failure(ip_key)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    if not verify_password(body.password, user.password_hash):
        await limiter.record_failure(user_key)
        await limiter.record_failure(ip_key)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    # Success: clear the username's failure tally so the legitimate
    # user starts fresh. Leave the IP bucket alone — a single correct
    # guess shouldn't reset network-wide enforcement (an attacker
    # would just rotate to other accounts).
    await limiter.clear(user_key)
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


class MeUsageKinds(BaseModel):
    """Per-model token breakdown for one model the user has touched."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


class MeUsage(BaseModel):
    """Token + turn counts for the requesting user over the last 30
    days. Queried from VictoriaMetrics (which holds the time-series
    drift-agent's in-process counters get scraped into via reporter-cp),
    so the number is persistent across drift-agent restarts.

    `models` carries the per-model breakdown — the frontend uses it to
    apply the right $/M-token pricing per provider before summing into
    a single cost number for the sidebar. The roll-up at the top level
    (`input_tokens`, etc.) is kept for back-compat with old clients
    that don't care about the breakdown.

    `window_days` makes the time horizon explicit on the wire so the
    UI's tooltip can label it; 30d strikes a reasonable balance between
    "recent activity" and "lifetime" for a sidebar gauge."""

    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    turns: int
    window_days: int
    models: dict[str, MeUsageKinds] = {}


@router.get("/me/usage", response_model=MeUsage)
async def me_usage(user: UserContext = Depends(get_current_user)) -> MeUsage:
    # Querying VM (rather than the in-process registry) gives us
    # restart-safe cumulative numbers: `increase()` detects counter
    # resets between samples and adds them back. drift-agent can rebuild
    # 100 times and this number keeps climbing.
    from ..config import settings as _settings
    from ..tools.metrics import make_vm_client

    window_days = 30
    _KIND_TO_ATTR = {
        "input": "input_tokens",
        "output": "output_tokens",
        "cache_read": "cache_read_input_tokens",
        "cache_creation": "cache_creation_input_tokens",
    }

    models: dict[str, MeUsageKinds] = {}
    totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    turns = 0

    if _settings.vm_url:
        vm = make_vm_client()
        try:
            # username is server-trusted (comes from the session), so
            # interpolating it into PromQL is safe — no need to escape.
            # The Prometheus label value lexer also doesn't permit
            # arbitrary code injection; worst case is an empty result.
            tokens_q = (
                f'sum by (model, kind) (increase(drift_agent_tokens_total'
                f'{{user="{user.username}"}}[{window_days}d]))'
            )
            turns_q = (
                f'sum(increase(drift_agent_turns_total'
                f'{{user="{user.username}"}}[{window_days}d]))'
            )
            tokens_resp = await vm.instant_query(tokens_q)
            for row in tokens_resp.get("data", {}).get("result", []) or []:
                metric = row.get("metric", {})
                model_name = metric.get("model") or "unknown"
                kind = metric.get("kind")
                attr = _KIND_TO_ATTR.get(kind)
                if attr is None:
                    continue
                try:
                    n = int(float(row.get("value", [0, "0"])[1]))
                except (ValueError, TypeError):
                    continue
                slot = models.setdefault(model_name, MeUsageKinds())
                setattr(slot, attr, getattr(slot, attr) + n)
                totals[attr] += n
            turns_resp = await vm.instant_query(turns_q)
            for row in turns_resp.get("data", {}).get("result", []) or []:
                try:
                    turns = int(float(row.get("value", [0, "0"])[1]))
                except (ValueError, TypeError):
                    pass
        except Exception:  # noqa: BLE001 — sidebar shouldn't error if VM blips
            pass
        finally:
            await vm.aclose()

    return MeUsage(
        input_tokens=totals["input_tokens"],
        output_tokens=totals["output_tokens"],
        cache_read_input_tokens=totals["cache_read_input_tokens"],
        cache_creation_input_tokens=totals["cache_creation_input_tokens"],
        turns=turns,
        window_days=window_days,
        models=models,
    )


@router.post("/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: PasswordChangeRequest,
    request: Request,
    actor: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Self-serve password change. Verifies the caller's current password,
    then updates the hash. Existing sessions stay valid — the caller is
    still authenticated under their current cookie.

    Rate-limited on the same buckets as /login (per-username + per-IP) so
    a stolen-session attacker can't quietly grind the current_password
    field to find leverage for a wider takeover."""
    # Same lockout posture as /login. Uses the authenticated user's
    # canonical username (not anything from the body) so an attacker
    # with a stolen cookie can't bypass by sending a different name.
    limiter = get_login_limiter()
    user_key = f"user:{actor.username.strip().lower()}"
    ip_key = f"ip:{client_ip_from_request(request)}"
    if await limiter.is_locked(user_key) or await limiter.is_locked(ip_key):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many failed password attempts, try again later",
            headers={"Retry-After": str(settings.login_failure_window_seconds)},
        )

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
        await limiter.record_failure(user_key)
        await limiter.record_failure(ip_key)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "current password is incorrect"
        )
    # Success: clear the username bucket so a legitimate user who
    # mistyped once isn't carrying a stale failure count.
    await limiter.clear(user_key)
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
