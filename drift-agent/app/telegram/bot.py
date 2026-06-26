"""Telegram bot loop: a chat UI for Drift's agent over long polling.

Each Telegram message lands in `handle_update`. Commands (`/start`, `/link`,
`/reset`) are intercepted; everything else is dispatched to Drift's
`run_agent` with `investigation_id=f"telegram:{chat_id}"`, which slots
straight into the agent's existing `_session_history` dict and gives us
per-chat conversation memory for free.

The agent yields SSE bytes; we parse them back into events and capture
narrative + markdown blocks into a single answer for the chat. Charts,
tables, timelines etc. are summarized as `[chart omitted — open Drift
to view]` so the operator knows there's more behind the answer if they
need it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from sqlalchemy import select

from .. import schemas as _schemas
from ..agent import _session_history, run_agent
from ..config import settings
from ..deploy.db import session as db_session
from ..deploy.models import User as UserModel, UserGroup
from ..users.deps import UserContext
from . import api as tgapi
from .links import redeem_code, user_for_chat


logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None


_GREETING = (
    "👋 I'm Drift's agent — observability, deploy state, alerts, all over chat.\n\n"
    "To connect this chat, get a /link code in the Drift web UI "
    "(or via `POST /api/telegram/link/code` if you're scripting), then send it here as:\n"
    "/link YOURCODE\n\n"
    "Once linked, ask me things like:\n"
    "• which devices look unhealthy right now?\n"
    "• show CPU for debian-8gb-ash-1 over the last hour\n"
    "• which containers are using the most memory?\n\n"
    "Commands: /reset clears this chat's short-term memory."
)


async def _reply(chat_id, text: str) -> None:
    await tgapi.send_message(chat_id, text)


async def _keep_typing(chat_id) -> None:
    """Re-send the 'typing…' action every few seconds so it stays visible
    while the agent thinks (Telegram auto-clears each one after ~5s)."""
    try:
        while True:
            await tgapi.send_chat_action(chat_id, "typing")
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        raise


# ---------- SSE → answer capture ----------


_SSE_EVENT = re.compile(rb"event: ([^\n]+)\ndata: (.*?)\n\n", re.S)


def _capture_block(block: dict, parts: list[str]) -> None:
    """Render a single agent-emitted block into chat-friendly markdown."""
    btype = block.get("type", "")
    if btype == "markdown":
        parts.append(block.get("content") or "")
        return
    if btype == "metric":
        label = block.get("label", "")
        val = block.get("value", "")
        unit = block.get("unit", "")
        trend = block.get("trend", "")
        line = f"**{label}**: {val}"
        if unit:
            line += f" {unit}"
        if trend:
            line += f" ({trend})"
        parts.append(line)
        return
    if btype == "table":
        title = block.get("title", "Table")
        rows = block.get("rows") or []
        cols = block.get("columns") or []
        if cols and rows:
            head = " | ".join(str(c) for c in cols)
            sep = " | ".join("---" for _ in cols)
            body = "\n".join(" | ".join(str(c) for c in r) for r in rows[:8])
            extra = f"\n_(showing 8 of {len(rows)} rows)_" if len(rows) > 8 else ""
            parts.append(f"**{title}**\n\n{head}\n{sep}\n{body}{extra}")
        else:
            parts.append(f"_{title} — (empty)_")
        return
    if btype == "timeline":
        title = block.get("title", "Timeline")
        events = block.get("events") or []
        if events:
            lines = [f"**{title}**"]
            for ev in events[:6]:
                lines.append(f"• {ev.get('label', '')} — {ev.get('time', '')}")
            if len(events) > 6:
                lines.append(f"_(+{len(events) - 6} more events)_")
            parts.append("\n".join(lines))
        return
    # chart / live_chart / terminal-action etc. — flag that there's more
    # detail in the SPA, but don't try to render here.
    parts.append(f"_[{btype} omitted — open Drift to view]_")


async def _user_context(user_row: UserModel) -> UserContext:
    """Build the UserContext shape run_agent expects from a User row.
    Mirrors get_current_user's tail (user → groups → snapshot) so a
    Telegram-driven turn has the same authorization scope as the same
    user logged into the SPA."""
    async with db_session() as s:
        groups = {
            g.group_id
            for g in (
                await s.execute(
                    select(UserGroup).where(UserGroup.user_id == user_row.id)
                )
            ).scalars().all()
        }
    return UserContext(
        id=user_row.id,
        username=user_row.username,
        role=user_row.role,
        groups=frozenset(groups),
    )


async def _run_agent_to_answer(prompt: str, user_row: UserModel, chat_id: str) -> str:
    """Drive Drift's run_agent for one turn and collapse the SSE event
    stream into a single chat-ready string. Reuses _session_history via
    investigation_id, so subsequent turns from this chat continue the
    same conversation."""
    investigation_id = f"telegram:{chat_id}"
    req = _schemas.PromptRequest(
        prompt=prompt,
        context=_schemas.PromptContext(investigation_id=investigation_id),
    )
    user_ctx = await _user_context(user_row)
    narrative_parts: list[str] = []
    block_parts: list[str] = []
    error: str | None = None
    buffer = b""
    async for chunk in run_agent(req, user=user_ctx):
        buffer += chunk
        while True:
            m = _SSE_EVENT.search(buffer)
            if not m:
                break
            event, raw = m.group(1).decode(), m.group(2)
            buffer = buffer[m.end():]
            try:
                data = json.loads(raw.decode())
            except Exception:  # noqa: BLE001
                continue
            if event == "narrative":
                narrative_parts.append(data.get("text", ""))
            elif event == "block":
                _capture_block(data, block_parts)
            elif event == "error":
                error = data.get("error", "unknown error")
    if error and not narrative_parts and not block_parts:
        return f"⚠️ {error}"
    body = "".join(narrative_parts).strip()
    if block_parts:
        body = (body + "\n\n" if body else "") + "\n\n".join(block_parts)
    return body or "_(no output)_"


# ---------- update dispatch ----------


async def handle_update(update: dict) -> None:
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return
    text = (msg.get("text") or "").strip()

    if text.startswith("/start"):
        # Deep link from t.me/<bot>?start=<code> arrives as "/start <code>".
        parts = text.split(maxsplit=1)
        payload = parts[1].strip() if len(parts) > 1 else ""
        if payload:
            title = chat.get("title") or chat.get("username") or chat.get("first_name")
            user_id = await redeem_code(payload, str(chat_id), title)
            await _reply(
                chat_id,
                "✅ Linked! This chat is now connected. Ask me anything about the fleet."
                if user_id
                else "That link is invalid or expired — get a fresh code in the Drift UI.",
            )
        else:
            await _reply(chat_id, _GREETING)
        return

    if text.startswith("/link"):
        parts = text.split(maxsplit=1)
        code = parts[1].strip() if len(parts) > 1 else ""
        title = chat.get("title") or chat.get("username") or chat.get("first_name")
        user_id = await redeem_code(code, str(chat_id), title)
        if user_id is None:
            await _reply(
                chat_id,
                "That code is invalid or expired. Get a fresh one in the Drift UI and try again.",
            )
        else:
            await _reply(
                chat_id,
                "✅ Linked! This chat is now connected. Ask me anything about the fleet.",
            )
        return

    if text.startswith("/reset"):
        _session_history.pop(f"telegram:{chat_id}", None)
        await _reply(chat_id, "Cleared this chat's short-term memory — starting fresh.")
        return

    if not text:
        return  # ignore stickers, photos, etc. (not supported yet)

    user_row = await user_for_chat(str(chat_id))
    if user_row is None:
        await _reply(
            chat_id,
            "This chat isn't linked yet. Send /link <code> with a code from the Drift UI.",
        )
        return

    typing = asyncio.create_task(_keep_typing(chat_id))
    try:
        answer = await _run_agent_to_answer(text, user_row, str(chat_id))
    except Exception as e:  # noqa: BLE001
        logger.exception("telegram: handling failed for chat %s", chat_id)
        answer = f"Sorry — something went wrong handling that ({type(e).__name__})."
    finally:
        typing.cancel()
        try:
            await typing
        except asyncio.CancelledError:
            pass
    await _reply(chat_id, answer)


# ---------- lifecycle ----------


async def _run_loop() -> None:
    me = await tgapi.get_me()
    logger.info(
        "telegram bot started%s",
        f" as @{me['username']}" if me and me.get("username") else "",
    )
    offset: int | None = None
    while True:
        try:
            updates = await tgapi.get_updates(offset, settings.telegram_poll_timeout)
            for up in updates:
                offset = up["update_id"] + 1
                try:
                    await handle_update(up)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "telegram: failed to handle update: %s: %s",
                        type(e).__name__, e,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "telegram getUpdates failed: %s: %s",
                type(e).__name__, e,
            )
            await asyncio.sleep(3)  # back off on transient API/network errors


def start_telegram_bot() -> None:
    """Spawn the long-poll loop. No-op if TELEGRAM_BOT_TOKEN is unset — the
    bot is opt-in and can be turned on simply by adding the token to .env
    and restarting drift-agent."""
    global _task
    if not settings.telegram_bot_token:
        logger.info("telegram: TELEGRAM_BOT_TOKEN not set, bot disabled")
        return
    if _task is not None and not _task.done():
        return
    loop = asyncio.get_event_loop()
    _task = loop.create_task(_run_loop())


async def stop_telegram_bot() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
