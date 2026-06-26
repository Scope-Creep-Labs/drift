"""Thin async Telegram Bot API client.

Ported from familai's `telegram_api.py` — generic, self-contained (httpx + a
bot token), no Drift-specific state. Only the calls the bot loop + alert
sink need: long-poll for updates, send a message, identify the bot. Telegram
message bodies are capped at 4096 chars, so callers should pre-truncate
(`send_message` does this for us).
"""
from __future__ import annotations

import html as _html
import logging
import re

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

_MAX_LEN = 3900  # leave headroom under Telegram's 4096 cap once entities are added


def to_telegram_html(md: str) -> str:
    """Convert the agent's GitHub-flavored markdown into the small HTML subset
    Telegram supports (<b>/<i>/<code>/<pre>/<a>). Heuristic but balanced-by-
    construction, so output is valid; `send_message` falls back to plain text
    if Telegram still rejects it."""
    s = _html.escape(md or "", quote=False)  # escape & < > (URLs get quotes handled below)
    s = re.sub(r"```[^\n`]*\n?(.*?)```", lambda m: "<pre>" + m.group(1).rstrip("\n") + "</pre>", s, flags=re.S)
    s = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", s)

    def _link(m: re.Match) -> str:
        return f'<a href="{m.group(2).replace(chr(34), "%22")}">{m.group(1)}</a>'

    s = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", _link, s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)          # **bold**
    s = re.sub(r"(?<!_)__(.+?)__(?!_)", r"<b>\1</b>", s)   # __bold__

    lines = []
    for line in s.split("\n"):
        heading = re.match(r"\s*#{1,6}\s+(.*)", line)
        if heading:
            lines.append("<b>" + heading.group(1).strip() + "</b>")
            continue
        bullet = re.match(r"(\s*)[-*+]\s+(.*)", line)
        if bullet:
            lines.append(bullet.group(1) + "• " + bullet.group(2))
            continue
        lines.append(line)
    s = "\n".join(lines)

    s = re.sub(r"(?<![\*\w])\*([^*\n]+)\*(?![\*\w])", r"<i>\1</i>", s)  # *italic*
    s = re.sub(r"(?<![_\w])_([^_\n]+)_(?![_\w])", r"<i>\1</i>", s)      # _italic_
    return s


def _strip_markdown(s: str) -> str:
    """Plain-text fallback: drop markdown markers so nothing renders literally."""
    s = re.sub(r"```[^\n`]*\n?(.*?)```", r"\1", s, flags=re.S)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"__(.+?)__", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"(?m)^\s*#{1,6}\s+", "", s)
    s = re.sub(r"(?m)^\s*[-*+]\s+", "• ", s)
    return s


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"


async def get_updates(offset: int | None, timeout: int) -> list[dict]:
    """Long-poll for new updates. Returns the `result` list (possibly empty)."""
    body: dict = {"timeout": timeout, "allowed_updates": ["message"]}
    if offset is not None:
        body["offset"] = offset
    # Client timeout must exceed the long-poll wait so the connection isn't cut.
    async with httpx.AsyncClient(timeout=timeout + 10) as client:
        r = await client.post(_api("getUpdates"), json=body)
    r.raise_for_status()
    data = r.json()
    return data.get("result", []) if data.get("ok") else []


async def _post_message(chat_id, text: str, parse_mode: str | None) -> tuple[bool, int]:
    body: dict = {"chat_id": chat_id, "text": text}
    if parse_mode:
        body["parse_mode"] = parse_mode
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(_api("sendMessage"), json=body)
        if r.status_code >= 400:
            logger.warning("telegram sendMessage to %s failed: HTTP %s %s", chat_id, r.status_code, r.text[:200])
            return False, r.status_code
        return True, r.status_code
    except Exception as e:  # noqa: BLE001
        logger.warning("telegram sendMessage to %s errored: %s: %s", chat_id, type(e).__name__, e)
        return False, 0


async def send_message(chat_id, text: str) -> bool:
    """Send a message, rendering the agent's markdown as Telegram HTML. Falls
    back to stripped plain text if Telegram rejects the HTML, so a formatting
    quirk never blocks delivery. Returns True on success."""
    text = (text or "").strip()[:_MAX_LEN] or "(no content)"
    ok, _ = await _post_message(chat_id, to_telegram_html(text), "HTML")
    if ok:
        return True
    ok, _ = await _post_message(chat_id, _strip_markdown(text), None)
    return ok


async def send_chat_action(chat_id, action: str = "typing") -> None:
    """Show a transient status (e.g. 'typing…') in the chat. Best-effort;
    Telegram auto-clears it after ~5s or when the next message arrives."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(_api("sendChatAction"), json={"chat_id": chat_id, "action": action})
    except Exception:  # noqa: BLE001
        pass


async def get_me() -> dict | None:
    """The bot's own account (for its @username), or None if unreachable."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(_api("getMe"))
        data = r.json()
        return data.get("result") if data.get("ok") else None
    except Exception:  # noqa: BLE001
        return None


_bot_username: str | None = None


async def bot_username() -> str | None:
    """The bot's @username (cached). Needed to build t.me/<bot>?start=<code> links."""
    global _bot_username
    if _bot_username is None:
        me = await get_me()
        if me:
            _bot_username = me.get("username")
    return _bot_username


async def deep_link(code: str) -> str | None:
    """Build the t.me deep link for a /link code (`t.me/<bot>?start=<code>`)
    so a user can tap a button instead of typing the code. Returns None if
    we couldn't fetch the bot's username (network blip, bad token)."""
    username = await bot_username()
    if not username:
        return None
    return f"https://t.me/{username}?start={code}"


def _qr_data_uri(data: str) -> str | None:
    """A PNG data: URI of `data` as a QR code, or None if segno isn't
    installed (older runtime image before v0.1.67). Frontends embed the
    returned string directly as `<img src={uri}>` — no extra fetch."""
    try:
        import segno
    except ImportError:
        return None
    return segno.make(data, error="m").png_data_uri(scale=5, border=2)


async def deep_link_and_qr(code: str) -> tuple[str | None, str | None]:
    """(deep_link, qr_data_uri) for a /link code. Either may be None: the
    deep link if the bot's username isn't resolvable (token bad / network);
    the QR if `segno` isn't installed. Callers fall back to the plain code
    so the flow degrades gracefully — operators can still type the code
    into the bot manually."""
    link = await deep_link(code)
    if not link:
        return None, None
    return link, _qr_data_uri(link)
