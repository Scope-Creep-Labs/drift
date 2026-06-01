"""Operator-learned noise-suppression filters.

The investigation agent calls these to evolve its behavior across
sessions: when the operator says "ignore that cadvisor product_name
error on pi-riffpod-001 in future reports," the agent calls
`remember_filter` to persist that intent. On the next investigation
scoped to the same device / group / container, the agent calls
`list_relevant_filters` first to load applicable rules, then suppresses
matches from its summary (and acknowledges in a small footer so the
operator can spot over-silencing).

Design notes:

- Filters are PER-USER (FK to users.id, ON DELETE CASCADE). Today there
  is no fleet-wide promote path; a future "promote to user-group" tool
  would copy the row for each member.
- Pattern matching is case-insensitive substring at READ time — the
  agent decides what counts as a "match" by reading the pattern out of
  list_relevant_filters and comparing in its own summary code path. No
  regex / wildcards in v1; keeps the surface small and avoids the need
  for a server-side matcher per signal type.
- Scope is a sparse JSONB dict: {"device", "container", "group",
  "signal"}. A request scope matches a filter scope iff every key the
  FILTER specifies equals the request's value (extra keys in the
  request are fine). Empty filter scope = user-wide.
- apply_count + last_applied_at give a usefulness signal — useful for
  "show me filters that haven't fired in 30d" follow-ups, and to help
  the agent self-prune stale rules.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update

from ..deploy.db import session
from ..deploy.models import OperatorFilter
from .metrics import ToolContext


_SCOPE_KEYS = ("device", "container", "group", "signal")


def _require_user(ctx: ToolContext) -> dict | None:
    """Filters are user-scoped. Refuse if no operator on the context."""
    if getattr(ctx, "user", None) is None:
        return {"error": "no authenticated user — filters are user-scoped and cannot be saved"}
    return None


def _normalize_scope(raw: Any) -> dict:
    """Pick known keys, coerce to lowercase strings (except container,
    which is preserved as-given since container_name is case-sensitive).
    Drop empty / non-string values so the resulting dict equality compare
    is reliable."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for k in _SCOPE_KEYS:
        v = raw.get(k)
        if not isinstance(v, str):
            continue
        v = v.strip()
        if not v:
            continue
        # container names are case-sensitive in docker; everything else
        # we store lowercase to match the device-name normalization
        # convention in deploy/naming.py.
        out[k] = v if k == "container" else v.lower()
    return out


def _scope_matches(filter_scope: dict, request_scope: dict) -> bool:
    """A filter applies if every key the filter pins matches the request.
    The request can carry extra keys; filter empty-keys are wildcards."""
    if not isinstance(filter_scope, dict):
        return False
    for k, v in filter_scope.items():
        if k not in _SCOPE_KEYS:
            continue
        if request_scope.get(k) != v:
            return False
    return True


async def remember_filter(ctx: ToolContext, args: dict) -> dict:
    if (err := _require_user(ctx)):
        return err
    pattern = (args.get("pattern") or "").strip()
    if not pattern:
        return {"error": "pattern is required and must be a non-empty string"}
    if len(pattern) > 4000:
        return {"error": "pattern too long (max 4000 chars). Pick a shorter substring."}
    scope = _normalize_scope(args.get("scope") or {})
    reason = (args.get("reason") or "").strip()
    if not reason:
        return {"error": "reason is required so future-you can judge whether the rule still applies"}

    async with session() as s:
        row = OperatorFilter(
            user_id=ctx.user.id,
            pattern=pattern,
            scope=scope,
            reason=reason,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)

    return {
        "filter_id": str(row.id),
        "pattern": row.pattern,
        "scope": row.scope,
        "reason": row.reason,
        "created_at": row.created_at.isoformat(),
        "note": (
            "Future investigations matching this scope will load this filter and "
            "suppress lines containing the pattern (case-insensitive substring)."
        ),
    }


async def list_relevant_filters(ctx: ToolContext, args: dict) -> dict:
    if (err := _require_user(ctx)):
        return err
    request_scope = _normalize_scope(args or {})

    async with session() as s:
        rows = (
            await s.execute(
                select(OperatorFilter).where(OperatorFilter.user_id == ctx.user.id)
            )
        ).scalars().all()

    matches = []
    matched_ids: list[uuid.UUID] = []
    for r in rows:
        if _scope_matches(r.scope or {}, request_scope):
            matches.append(
                {
                    "id": str(r.id),
                    "pattern": r.pattern,
                    "scope": r.scope or {},
                    "reason": r.reason or "",
                    "created_at": r.created_at.isoformat(),
                    "last_applied_at": (
                        r.last_applied_at.isoformat() if r.last_applied_at else None
                    ),
                    "apply_count": r.apply_count,
                }
            )
            matched_ids.append(r.id)

    # Best-effort usefulness bump. Failure here doesn't block the
    # response — the agent already has the filters it needs.
    if matched_ids:
        try:
            now = datetime.now(timezone.utc)
            async with session() as s:
                await s.execute(
                    update(OperatorFilter)
                    .where(OperatorFilter.id.in_(matched_ids))
                    .values(
                        last_applied_at=now,
                        apply_count=OperatorFilter.apply_count + 1,
                    )
                )
                await s.commit()
        except Exception:
            pass

    return {
        "n": len(matches),
        "request_scope": request_scope,
        "filters": matches,
    }


async def forget_filter(ctx: ToolContext, args: dict) -> dict:
    if (err := _require_user(ctx)):
        return err
    raw_id = (args.get("filter_id") or "").strip()
    if not raw_id:
        return {"error": "filter_id is required"}
    try:
        fid = uuid.UUID(raw_id)
    except ValueError:
        return {"error": f"filter_id '{raw_id}' is not a valid UUID"}

    async with session() as s:
        row = (
            await s.execute(
                select(OperatorFilter).where(
                    OperatorFilter.id == fid,
                    OperatorFilter.user_id == ctx.user.id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return {"error": f"filter '{raw_id}' not found (or not owned by this user)"}
        pattern_for_echo = row.pattern
        await s.delete(row)
        await s.commit()

    return {
        "filter_id": str(fid),
        "deleted": True,
        "pattern": pattern_for_echo,
    }


# ---------- Tool schemas (Claude tool-use shape) ----------


FILTERS_TOOLS: list[dict] = [
    {
        "name": "remember_filter",
        "description": (
            "Persist an operator-supplied rule to suppress a recurring noise pattern in "
            "FUTURE investigations. Call when the operator says things like 'ignore X', "
            "'treat Y as known noise', 'don't report Z', or 'make a note to skip that'. "
            "The pattern is later matched as a case-insensitive substring against log / "
            "error / alert text. Always pass a `reason` (the operator's WHY) — it helps "
            "you and the operator decide later whether the rule is still relevant."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": (
                        "Substring to suppress (case-insensitive). Pick a span specific "
                        "enough to not over-silence, e.g. 'product_name: no such file' "
                        "rather than just 'no such file'."
                    ),
                },
                "scope": {
                    "type": "object",
                    "description": (
                        "Narrowing scope. Any combination of keys; an absent key acts "
                        "as a wildcard. Empty {} means 'user-wide'."
                    ),
                    "properties": {
                        "device": {
                            "type": "string",
                            "description": "Device name (same string as host in metrics).",
                        },
                        "container": {
                            "type": "string",
                            "description": "Container name (case-sensitive, e.g. 'cadvisor').",
                        },
                        "group": {
                            "type": "string",
                            "description": "group_id when the rule should apply to a fleet group.",
                        },
                        "signal": {
                            "type": "string",
                            "description": "'log' | 'metric' | 'alert' — coarse signal-kind hint.",
                        },
                    },
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "One-line WHY the operator marked this as noise. Stored verbatim — "
                        "future you reads this to decide whether the rule still applies."
                    ),
                },
            },
            "required": ["pattern", "reason"],
        },
    },
    {
        "name": "list_relevant_filters",
        "description": (
            "Return the operator's noise-suppression filters that apply to the current "
            "investigation scope. ALWAYS call this at the START of any investigation that "
            "scopes to a device, group, or specific container — BEFORE you summarize "
            "errors, alerts, or noisy logs. The result is a small list; check each "
            "filter's `pattern` against the lines you'd otherwise report and suppress "
            "matches (case-insensitive substring). Acknowledge in a small footer like "
            "'_suppressed N lines matching M filters_' so the operator can spot "
            "over-silencing. Empty result = no rules for this scope; proceed normally."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "device": {
                    "type": "string",
                    "description": "Device name in the investigation scope.",
                },
                "container": {
                    "type": "string",
                    "description": "Container name when narrowing to a single service.",
                },
                "group": {
                    "type": "string",
                    "description": "group_id when investigating a logical fleet group.",
                },
                "signal": {
                    "type": "string",
                    "description": "'log' | 'metric' | 'alert' — coarse signal-kind hint.",
                },
            },
        },
    },
    {
        "name": "forget_filter",
        "description": (
            "Remove a previously-remembered filter. Call when the operator says 'stop "
            "ignoring X', 'remove that filter', 'I was wrong about Y'. Use the `id` "
            "field returned by `list_relevant_filters` or `remember_filter`. Filters are "
            "personal — operators can only delete their own."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filter_id": {
                    "type": "string",
                    "description": "UUID returned when the filter was created or listed.",
                },
            },
            "required": ["filter_id"],
        },
    },
]


FILTERS_HANDLERS: dict = {
    "remember_filter": remember_filter,
    "list_relevant_filters": list_relevant_filters,
    "forget_filter": forget_filter,
}
