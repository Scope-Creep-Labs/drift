"""Chat ↔ Drift account binding for the Telegram bot.

Adapted from familai's `telegram_links.py` to Drift's pg + SQLAlchemy
async session pattern. Two operations:

  - issue_link_code(user_id)  → row in `telegram_link_codes`, ~10min TTL.
                                The user types `/link <code>` (or scans
                                the QR / taps the deep link) to redeem.
  - redeem_code(code, chat_id) → moves the binding to `telegram_chats`,
                                  deletes the code. Returns the user_id
                                  bound (or None on invalid/expired).

Plus the read-side lookups the bot needs (`user_for_chat`) and a few
admin helpers (`unlink_chat`, `list_chats_for_user`).
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from ..config import settings
from ..deploy.db import session as db_session
from ..deploy.models import TelegramChat, TelegramLinkCode, User


# Unambiguous code alphabet (no 0/O/1/I/L) — codes are read aloud / retyped.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _gen_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(6))


async def issue_link_code(user_id: uuid.UUID) -> dict:
    """Create a one-time code the user sends to the bot. Any prior
    unredeemed code for this user is invalidated (one outstanding code
    per user — operator-side, the SPA shows the latest)."""
    code = _gen_code()
    expires = _now() + timedelta(minutes=settings.telegram_link_code_ttl_min)
    async with db_session() as s:
        await s.execute(
            delete(TelegramLinkCode).where(TelegramLinkCode.user_id == user_id)
        )
        s.add(
            TelegramLinkCode(
                code=code, user_id=user_id, expires_at=expires
            )
        )
        await s.commit()
    return {
        "code": code,
        "expires_at": expires.isoformat(),
        "expires_min": settings.telegram_link_code_ttl_min,
    }


async def redeem_code(
    code: str, chat_id: str, title: str | None
) -> uuid.UUID | None:
    """Bind `chat_id` to the code's user. Returns the user_id on success
    (and consumes the code), or None if the code is unknown/expired.
    Re-binding an already-linked chat_id swaps it to the new user — same
    as familai's ON CONFLICT DO UPDATE."""
    code = (code or "").strip().upper()
    if not code:
        return None
    chat_id = str(chat_id)
    async with db_session() as s:
        row = (
            await s.execute(
                select(TelegramLinkCode).where(TelegramLinkCode.code == code)
            )
        ).scalar_one_or_none()
        if row is None or row.expires_at < _now():
            return None
        user_id = row.user_id
        # Upsert into telegram_chats — same chat may already be linked
        # to a different account (operator handing over to a new admin).
        existing = await s.get(TelegramChat, chat_id)
        if existing is None:
            s.add(
                TelegramChat(
                    chat_id=chat_id, user_id=user_id, title=title
                )
            )
        else:
            existing.user_id = user_id
            existing.title = title
        # Consume the code.
        await s.execute(
            delete(TelegramLinkCode).where(TelegramLinkCode.code == code)
        )
        await s.commit()
        return user_id


async def user_for_chat(chat_id: str) -> User | None:
    """The Drift user bound to a chat, or None if unlinked. Returns the
    full User row so the bot loop can pass role + is_admin downstream
    without extra lookups."""
    chat_id = str(chat_id)
    async with db_session() as s:
        row = (
            await s.execute(
                select(User)
                .join(TelegramChat, TelegramChat.user_id == User.id)
                .where(TelegramChat.chat_id == chat_id)
            )
        ).scalar_one_or_none()
        return row


async def list_chats_for_user(user_id: uuid.UUID) -> list[dict]:
    """All chats currently linked to this user — for the SPA's account
    settings panel ('My linked Telegram chats')."""
    async with db_session() as s:
        rows = (
            await s.execute(
                select(TelegramChat).where(TelegramChat.user_id == user_id)
            )
        ).scalars().all()
        return [
            {
                "chat_id": r.chat_id,
                "title": r.title,
                "linked_at": r.linked_at.isoformat(),
            }
            for r in rows
        ]


async def unlink_chat(chat_id: str, user_id: uuid.UUID | None = None) -> bool:
    """Remove a binding. If user_id is given, only delete if the binding
    actually belongs to that user (used so a non-admin can only unlink
    their own chats). Returns True if a row was deleted."""
    chat_id = str(chat_id)
    async with db_session() as s:
        existing = await s.get(TelegramChat, chat_id)
        if existing is None:
            return False
        if user_id is not None and existing.user_id != user_id:
            return False
        await s.delete(existing)
        await s.commit()
        return True
