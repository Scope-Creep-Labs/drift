"""User management tools (admin only).

Mirrors the device-commissioning pattern: when creating or resetting a
user, the server generates the password and returns it ONCE in the
tool response. The operator passes it to the user out-of-band; nothing
else in Drift ever sees the plaintext.

Caveat the operator should know: the generated password DOES land in
the chat trace (and therefore localStorage and the LLM's working
context). This is acceptable in a single-operator setup but worth
being mindful of. For stricter setups, the parallel UI modal flow
(planned follow-up) keeps the password off the chat altogether.
"""
from __future__ import annotations

import secrets
import string
import uuid

from sqlalchemy import select

from ..config import settings
from ..deploy.db import session
from ..deploy.models import User, UserGroup
from ..users.passwords import hash_password
from .metrics import ToolContext


_VALID_ROLES = ("observe", "deploy", "admin")


def _generate_password() -> str:
    """16 alphanumeric chars, ~95 bits of entropy. Excludes look-alike
    glyphs (0/O, 1/l/I) so operators can paste-without-mistyping into
    chat messages."""
    alphabet = "".join(
        c for c in string.ascii_letters + string.digits if c not in "0O1lI"
    )
    return "".join(secrets.choice(alphabet) for _ in range(16))


def _require_admin(ctx: ToolContext) -> dict | None:
    """User management is admin-only. Returns an error dict if not."""
    user = getattr(ctx, "user", None)
    if user is None:
        return None  # test/dev context — allow
    if not user.is_admin:
        return {
            "error": (
                f"permission denied: operator '{user.username}' has role '{user.role}'. "
                "User management requires the 'admin' role."
            )
        }
    return None


def _refuse_in_demo(action: str) -> dict | None:
    """Refuse user-management mutations in DEMO_MODE. Listing remains
    allowed (read-only)."""
    if settings.demo_mode:
        return {
            "error": (
                f"DEMO_MODE blocked {action}. User accounts are managed out-of-band "
                "for this demo deployment."
            ),
        }
    return None


async def list_users(ctx: ToolContext, _args: dict) -> dict:
    if (err := _require_admin(ctx)):
        return err
    async with session() as s:
        rows = (await s.execute(select(User).order_by(User.username))).scalars().all()
        users = []
        for u in rows:
            groups = (
                await s.execute(select(UserGroup.group_id).where(UserGroup.user_id == u.id))
            ).scalars().all()
            users.append({
                "username": u.username,
                "role": u.role,
                "groups": sorted(groups),
                "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
                "created_at": u.created_at.isoformat(),
            })
    return {"n": len(users), "users": users}


async def create_user(ctx: ToolContext, args: dict) -> dict:
    if (err := _require_admin(ctx)):
        return err
    if (err := _refuse_in_demo("create_user")):
        return err
    username = (args.get("username") or "").strip()
    role = (args.get("role") or "observe").strip()
    groups = args.get("groups") or []
    if not username:
        return {"error": "username is required"}
    if role not in _VALID_ROLES:
        return {"error": f"role must be one of {list(_VALID_ROLES)}; got '{role}'"}
    if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
        return {"error": "groups must be a list of strings"}

    password = _generate_password()

    async with session() as s:
        existing = (
            await s.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if existing is not None:
            return {"error": f"user '{username}' already exists (use reset_user_password to give them a new password)"}
        user = User(
            username=username,
            password_hash=hash_password(password),
            role=role,
        )
        s.add(user)
        await s.flush()
        for g in groups:
            s.add(UserGroup(user_id=user.id, group_id=g))
        await s.commit()

    return {
        "username": username,
        "role": role,
        "groups": sorted(groups),
        "password": password,
        "note": (
            "Pass the password to the user out-of-band (Slack/email). The "
            "operator's chat trace stores it — clear the investigation "
            "afterwards if that's a concern. The user can't change their "
            "own password in v1; if it leaks, use reset_user_password."
        ),
    }


async def set_user_role(ctx: ToolContext, args: dict) -> dict:
    if (err := _require_admin(ctx)):
        return err
    username = (args.get("username") or "").strip()
    role = (args.get("role") or "").strip()
    if not username or role not in _VALID_ROLES:
        return {"error": f"username and role (one of {list(_VALID_ROLES)}) are required"}

    actor = getattr(ctx, "user", None)
    async with session() as s:
        user = (
            await s.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if user is None:
            return {"error": f"user '{username}' not found"}
        prior_role = user.role
        # Last-admin protection: refuse to demote the only admin, even
        # if it's not the actor themselves.
        if prior_role == "admin" and role != "admin":
            other_admin = (
                await s.execute(
                    select(User).where(User.role == "admin", User.id != user.id)
                )
            ).scalars().first()
            if other_admin is None:
                return {"error": "cannot demote the last admin — create another admin first"}
        user.role = role
        await s.commit()

    return {
        "username": username,
        "prior_role": prior_role,
        "role": role,
        "note": "Active sessions for this user are NOT invalidated; they keep their old permissions until logout or 30-day session expiry. Have them log out + back in for the change to apply.",
    }


async def set_user_groups(ctx: ToolContext, args: dict) -> dict:
    if (err := _require_admin(ctx)):
        return err
    username = (args.get("username") or "").strip()
    groups = args.get("groups")
    if not username:
        return {"error": "username is required"}
    if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
        return {"error": "groups must be a list of strings (use [] to remove all groups)"}

    async with session() as s:
        user = (
            await s.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if user is None:
            return {"error": f"user '{username}' not found"}
        prior = (
            await s.execute(select(UserGroup.group_id).where(UserGroup.user_id == user.id))
        ).scalars().all()
        await s.execute(
            UserGroup.__table__.delete().where(UserGroup.user_id == user.id)
        )
        for g in groups:
            s.add(UserGroup(user_id=user.id, group_id=g))
        await s.commit()

    return {
        "username": username,
        "prior_groups": sorted(prior),
        "groups": sorted(groups),
    }


async def reset_user_password(ctx: ToolContext, args: dict) -> dict:
    if (err := _require_admin(ctx)):
        return err
    if (err := _refuse_in_demo("reset_user_password")):
        return err
    username = (args.get("username") or "").strip()
    if not username:
        return {"error": "username is required"}
    password = _generate_password()
    async with session() as s:
        user = (
            await s.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if user is None:
            return {"error": f"user '{username}' not found"}
        user.password_hash = hash_password(password)
        await s.commit()
    return {
        "username": username,
        "password": password,
        "note": "Existing sessions are NOT revoked — only future logins use the new password. To force re-login, delete the user's session rows in Postgres.",
    }


async def delete_user(ctx: ToolContext, args: dict) -> dict:
    if (err := _require_admin(ctx)):
        return err
    if (err := _refuse_in_demo("delete_user")):
        return err
    username = (args.get("username") or "").strip()
    if not username:
        return {"error": "username is required"}
    actor = getattr(ctx, "user", None)
    if actor is not None and actor.username == username:
        return {"error": "cannot delete your own account — ask another admin"}

    async with session() as s:
        user = (
            await s.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if user is None:
            return {"error": f"user '{username}' not found"}
        if user.role == "admin":
            other_admin = (
                await s.execute(
                    select(User).where(User.role == "admin", User.id != user.id)
                )
            ).scalars().first()
            if other_admin is None:
                return {"error": "cannot delete the last admin"}
        await s.delete(user)
        await s.commit()

    return {"username": username, "deleted": True}


# ---------- Tool schemas (Claude tool-use shape) ----------


USER_TOOLS: list[dict] = [
    {
        "name": "list_users",
        "description": (
            "List all Drift users. Admin only. Returns username, role, groups, and "
            "last_login_at per row. No passwords (we never store plaintext)."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_user",
        "description": (
            "Create a new Drift user. Admin only. The server generates a random 16-char "
            "password and returns it once — pass it to the user out-of-band. The user "
            "cannot self-change their password in v1; admin uses reset_user_password "
            "if it leaks. Default role is 'observe' (read + alert management). Groups "
            "is a list of device groups (e.g. ['drift_home'])."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "username": {"type": "string"},
                "role": {
                    "type": "string",
                    "enum": ["observe", "deploy", "admin"],
                    "description": "Default 'observe'. observe ⊂ deploy ⊂ admin.",
                },
                "groups": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Device groups the user can manage. Admins get implicit access to all groups regardless.",
                },
            },
            "required": ["username"],
        },
    },
    {
        "name": "set_user_role",
        "description": (
            "Change a user's role. Admin only. Last-admin protection: can't demote the "
            "only admin. Note: existing sessions keep their old permissions until next "
            "login (or 30-day expiry)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "username": {"type": "string"},
                "role": {
                    "type": "string",
                    "enum": ["observe", "deploy", "admin"],
                },
            },
            "required": ["username", "role"],
        },
    },
    {
        "name": "set_user_groups",
        "description": (
            "Replace a user's group memberships. Admin only. Pass an empty list to "
            "remove all groups (turning the user into a deploy/observe-with-no-targets "
            "role — they can still see apps but won't see any devices)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "username": {"type": "string"},
                "groups": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["username", "groups"],
        },
    },
    {
        "name": "reset_user_password",
        "description": (
            "Generate a fresh random password for a user and return it once. Admin "
            "only. Existing sessions are NOT revoked automatically — only future "
            "logins use the new password."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"username": {"type": "string"}},
            "required": ["username"],
        },
    },
    {
        "name": "delete_user",
        "description": (
            "Delete a user. Admin only. Refuses to delete the operator themselves or "
            "the last admin. Cascade: all sessions and group memberships go with it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"username": {"type": "string"}},
            "required": ["username"],
        },
    },
]


USER_HANDLERS = {
    "list_users": list_users,
    "create_user": create_user,
    "set_user_role": set_user_role,
    "set_user_groups": set_user_groups,
    "reset_user_password": reset_user_password,
    "delete_user": delete_user,
}
