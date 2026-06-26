"""Alertmanager → Telegram bridge.

Alertmanager fires its webhook receiver at this CP's endpoint
`/api/telegram/alertmanager/webhook?secret=<TELEGRAM_WEBHOOK_SECRET>`,
sending one POST per alert group with the structure documented at
https://prometheus.io/docs/alerting/latest/configuration/#webhook_config.
We translate each alert in `.alerts[]` into a Telegram-formatted message
and `send_message` it to every chat in `TELEGRAM_ALERT_CHATS`.

Why a separate sink (and not replace `alertmanager-ntfy`): Drift's
existing ntfy bridge stays untouched — operators on phones without
Telegram keep their notification path. This is a sibling, not a swap.
"""
from __future__ import annotations

import logging
from typing import Any

from ..config import settings
from . import api as tgapi


logger = logging.getLogger(__name__)


def _format_alert(alert: dict[str, Any]) -> str:
    """Render one Alertmanager alert into a Telegram-friendly markdown
    snippet. Order: severity + alertname headline, then summary, then
    description, then a footer line of labels worth surfacing."""
    labels = alert.get("labels") or {}
    annotations = alert.get("annotations") or {}
    status = alert.get("status", "firing")  # firing | resolved
    sev = (labels.get("severity") or "").lower() or "warning"

    icon = {
        "critical": "🚨",
        "page": "🚨",
        "warning": "⚠️",
        "info": "ℹ️",
    }.get(sev, "🔔")
    if status == "resolved":
        icon = "✅"

    alertname = labels.get("alertname") or "(unnamed alert)"
    headline = f"{icon} *{alertname}*"
    if status == "resolved":
        headline += " — *resolved*"
    elif sev:
        headline += f" — {sev}"

    parts = [headline]

    summary = annotations.get("summary")
    if summary:
        parts.append(summary)
    description = annotations.get("description")
    if description and description != summary:
        parts.append(description)

    # Footer: a short, useful label list. Pin the ones operators usually
    # care about; drop the rest to keep the message short.
    pinned = ["host", "device", "instance", "service", "job", "group"]
    footer_bits = []
    for k in pinned:
        v = labels.get(k)
        if v:
            footer_bits.append(f"`{k}={v}`")
    if footer_bits:
        parts.append(" ".join(footer_bits))

    return "\n".join(parts)


async def deliver_alertmanager_webhook(body: dict[str, Any]) -> dict[str, Any]:
    """Fan out one Alertmanager webhook body to all configured chats.
    Returns a summary dict (delivered counts) for the HTTP response so
    the operator can see what happened in Alertmanager's own UI."""
    alerts = body.get("alerts") or []
    chats = settings.telegram_alert_chats_list
    if not chats:
        logger.info("telegram: webhook received but TELEGRAM_ALERT_CHATS is empty")
        return {"delivered": 0, "alerts": len(alerts), "chats": 0}

    delivered = 0
    failed = 0
    for alert in alerts:
        text = _format_alert(alert)
        for chat_id in chats:
            ok = await tgapi.send_message(chat_id, text)
            if ok:
                delivered += 1
            else:
                failed += 1

    return {
        "delivered": delivered,
        "failed": failed,
        "alerts": len(alerts),
        "chats": len(chats),
    }
