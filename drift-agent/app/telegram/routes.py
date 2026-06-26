"""FastAPI surface for the Telegram feature.

Two halves:

- **Operator-facing** (cookie-auth via the SPA): mint a `/link` code,
  list your linked chats, unlink one. Mounted under `/api/telegram`.

- **Alertmanager-facing** (shared-secret URL): a single POST endpoint
  that translates the webhook body into Telegram messages. Anonymous
  (Alertmanager doesn't do bearer auth out of the box) but gated by
  `?secret=…` matching `TELEGRAM_WEBHOOK_SECRET`.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

from ..config import settings
from ..users.deps import UserContext, get_current_user
from . import api as tgapi
from . import alerts as tgalerts
from . import links as tglinks


router = APIRouter(prefix="/api/telegram", tags=["telegram"])


# ---------- helpers ----------


def _feature_or_503() -> None:
    if not settings.telegram_bot_token:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "telegram feature is disabled — set TELEGRAM_BOT_TOKEN in the CP env",
        )


# ---------- operator surface ----------


@router.post("/link/code")
async def create_link_code(
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    """Mint a one-time code to bind the caller's Drift account to a
    Telegram chat. The user types `/link <code>` in the bot (or taps
    the deep link). Codes expire after TELEGRAM_LINK_CODE_TTL_MIN minutes.
    Any prior unredeemed code for this user is invalidated."""
    _feature_or_503()
    payload = await tglinks.issue_link_code(user.id)
    link, qr = await tgapi.deep_link_and_qr(payload["code"])
    if link:
        payload["deep_link"] = link
    if qr:
        payload["qr_data_uri"] = qr
    return payload


@router.get("/chats")
async def list_chats(
    user: UserContext = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """All Telegram chats linked to the current user."""
    _feature_or_503()
    return await tglinks.list_chats_for_user(user.id)


@router.delete("/chats/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unlink_chat(
    chat_id: str,
    user: UserContext = Depends(get_current_user),
) -> None:
    """Remove a binding the caller owns. Admins can unlink any chat by
    passing user_id=None; we keep the bar at 'must own this chat' here
    so a deploy-role user can't yank an admin's link."""
    _feature_or_503()
    scope = None if user.is_admin else user.id
    ok = await tglinks.unlink_chat(chat_id, user_id=scope)
    if not ok:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"chat '{chat_id}' is not linked to you",
        )


# ---------- alertmanager surface ----------


@router.post(
    "/alertmanager/webhook",
    include_in_schema=False,  # internal — keep out of the OpenAPI surface
)
async def alertmanager_webhook(
    body: dict[str, Any] = Body(...),
    secret: str = Query(default=""),
) -> dict[str, Any]:
    """Alertmanager fires here. Auth is the shared `?secret=…` query
    param — there's no bearer/cookie path that survives Alertmanager's
    config cleanly, and the endpoint is exposed only over HTTPS through
    the same Caddy as the rest of /api so the secret stays opaque on
    the wire.

    A 404 (vs 401/403) on bad-secret hides the endpoint's existence
    from someone fuzzing — the same posture as 'not found' in our
    on-demand-TLS ask hook.
    """
    expected = settings.telegram_webhook_secret
    if not expected or secret != expected:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if not settings.telegram_bot_token:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "telegram feature is disabled",
        )
    return await tgalerts.deliver_alertmanager_webhook(body)
