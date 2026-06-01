"""Operator-learned noise-suppression filters.

The investigation agent calls these to evolve its behavior across
sessions: when the operator says "ignore that cadvisor product_name
error on pi-riffpod-001 in future reports," the agent calls
`remember_filter` to persist that intent. On the next investigation
scoped to the same device / group / container, the agent calls
`list_relevant_filters` first to load applicable rules, then suppresses
matches from its summary (and acknowledges in a small footer so the
operator can spot over-silencing).

Filter visibility (v0.1.49+):

- 'private' (default) — only the owning user sees it. Created by
  remember_filter; deleted by forget_filter; owner can promote.
- 'fleet'   — visible to every authenticated operator. Promoted by any
  user via promote_filter. Only the original creator can forget
  (hard-delete); other users see it in lookups but can't revoke it.

Scope matching is COMPATIBLE-scope (lenient): a filter applies when no
key pinned by BOTH has conflicting values. Keys the filter pins that
the request omits are kept — the agent checks those constraints
per-line against each log/error's actual source.

Pattern matching is case-insensitive substring at READ time — the
agent decides what counts as a "match" per-line. No regex / no
wildcards in v1; keeps the surface small and avoids ReDoS.

Dedup: remember_filter + promote_filter both check against the set of
filters visible to the caller (own private + all fleet) for an
existing row with the same normalized (pattern, scope). If found,
return the existing row instead of creating a duplicate.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..deploy.db import session
from ..deploy.models import OperatorFilter
from .metrics import ToolContext


_SCOPE_KEYS = ("device", "container", "group", "signal")
_VISIBILITY_PRIVATE = "private"
_VISIBILITY_FLEET = "fleet"
_VALID_VISIBILITIES = (_VISIBILITY_PRIVATE, _VISIBILITY_FLEET)


# ---------- helpers ----------


def _require_user(ctx: ToolContext) -> dict | None:
    if getattr(ctx, "user", None) is None:
        return {"error": "no authenticated user — filters are user-scoped and cannot be saved"}
    return None


def _normalize_scope(raw: Any) -> dict:
    """Pick known keys; coerce to stripped strings. device/group/signal are
    lowered (we normalize device names that way elsewhere). container is
    preserved as-given since container_name is case-sensitive in docker."""
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
        out[k] = v if k == "container" else v.lower()
    return out


def _scope_matches(filter_scope: dict, request_scope: dict) -> bool:
    """A filter is COMPATIBLE with the request when no key pinned by both
    has conflicting values. Keys the filter pins that the request omits
    are NOT a disqualifier — the agent applies those constraints per-line
    against the actual log/error source.

    Examples (filter scope → request scope → match?):
      {device: pi, signal: log}     {device: pi}                     → ✓
      {device: pi, signal: log}     {device: pi, signal: alert}      → ✗
      {device: pi, container: x}    {device: pi}                     → ✓
      {device: pi}                  {device: pi, container: x}       → ✓
      {}                            {device: pi}                     → ✓
    """
    if not isinstance(filter_scope, dict):
        return False
    for k, v in filter_scope.items():
        if k not in _SCOPE_KEYS:
            continue
        rv = request_scope.get(k)
        if rv is not None and rv != v:
            return False
    return True


def _pattern_canon(p: str) -> str:
    return (p or "").strip().casefold()


def _scope_equal(a: dict, b: dict) -> bool:
    a = a or {}
    b = b or {}
    return {k: a.get(k) for k in _SCOPE_KEYS} == {k: b.get(k) for k in _SCOPE_KEYS}


async def _find_duplicate(
    s: AsyncSession,
    *,
    user_id: uuid.UUID,
    pattern: str,
    scope: dict,
) -> Optional[OperatorFilter]:
    """Return any existing filter visible to this user (own private OR any
    fleet) with the same normalized (pattern, scope). Used by both
    remember_filter and promote_filter to dedup."""
    visible_rows = (
        await s.execute(
            select(OperatorFilter).where(
                or_(
                    (OperatorFilter.user_id == user_id)
                    & (OperatorFilter.visibility == _VISIBILITY_PRIVATE),
                    OperatorFilter.visibility == _VISIBILITY_FLEET,
                )
            )
        )
    ).scalars().all()
    canon_p = _pattern_canon(pattern)
    for r in visible_rows:
        if _pattern_canon(r.pattern) == canon_p and _scope_equal(r.scope or {}, scope):
            return r
    return None


def _serialize_filter(r: OperatorFilter, *, viewer_id: Optional[uuid.UUID] = None) -> dict:
    return {
        "id": str(r.id),
        "pattern": r.pattern,
        "scope": r.scope or {},
        "reason": r.reason or "",
        "visibility": r.visibility,
        "owned_by_me": (viewer_id is not None and r.user_id == viewer_id),
        "created_at": r.created_at.isoformat(),
        "last_applied_at": r.last_applied_at.isoformat() if r.last_applied_at else None,
        "apply_count": r.apply_count,
    }


# ---------- tool handlers ----------


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
        # Dedup: if an equivalent filter is already visible to this
        # user (own private or any fleet), return it instead of
        # inserting a duplicate.
        dup = await _find_duplicate(s, user_id=ctx.user.id, pattern=pattern, scope=scope)
        if dup is not None:
            return {
                **_serialize_filter(dup, viewer_id=ctx.user.id),
                "deduped": True,
                "note": (
                    f"An equivalent {dup.visibility} filter already exists "
                    f"({'yours' if dup.user_id == ctx.user.id else 'fleet-shared'}); "
                    "returning that one instead of creating a duplicate."
                ),
            }
        row = OperatorFilter(
            user_id=ctx.user.id,
            pattern=pattern,
            scope=scope,
            reason=reason,
            visibility=_VISIBILITY_PRIVATE,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)

    return {
        **_serialize_filter(row, viewer_id=ctx.user.id),
        "deduped": False,
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
                select(OperatorFilter).where(
                    or_(
                        (OperatorFilter.user_id == ctx.user.id)
                        & (OperatorFilter.visibility == _VISIBILITY_PRIVATE),
                        OperatorFilter.visibility == _VISIBILITY_FLEET,
                    )
                )
            )
        ).scalars().all()

    matches: list[dict] = []
    matched_ids: list[uuid.UUID] = []
    for r in rows:
        if _scope_matches(r.scope or {}, request_scope):
            matches.append(_serialize_filter(r, viewer_id=ctx.user.id))
            matched_ids.append(r.id)

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
            await s.execute(select(OperatorFilter).where(OperatorFilter.id == fid))
        ).scalar_one_or_none()
        if row is None:
            return {"error": f"filter '{raw_id}' not found"}
        if row.user_id != ctx.user.id:
            return {
                "error": (
                    f"filter '{raw_id}' is {row.visibility} and owned by another operator. "
                    "You can only forget filters you created. If a fleet filter is over-silencing, "
                    "ask the creator (or an admin) to forget it."
                ),
            }
        pattern_for_echo = row.pattern
        visibility_for_echo = row.visibility
        await s.delete(row)
        await s.commit()

    return {
        "filter_id": str(fid),
        "deleted": True,
        "pattern": pattern_for_echo,
        "visibility": visibility_for_echo,
    }


async def promote_filter(ctx: ToolContext, args: dict) -> dict:
    """Convert a private filter to fleet-wide visibility.

    Any authenticated user can promote ANY filter visible to them
    (own private OR any private the operator-controlled UI surfaces —
    but the chat tool flow only sees the caller's own private rows
    plus all fleet rows, so in practice only own private filters get
    promoted here). The original creator remains as user_id; that's
    the audit trail. Dedup: if an equivalent fleet filter already
    exists, the source filter is deleted and the existing fleet
    filter is returned (so the operator doesn't end up with two rows
    they think are different).
    """
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
            await s.execute(select(OperatorFilter).where(OperatorFilter.id == fid))
        ).scalar_one_or_none()
        if row is None:
            return {"error": f"filter '{raw_id}' not found"}
        if row.visibility == _VISIBILITY_FLEET:
            return {
                **_serialize_filter(row, viewer_id=ctx.user.id),
                "already_fleet": True,
                "note": "Filter is already fleet-wide — no change.",
            }

        # Dedup against existing fleet filters with the same pattern +
        # scope. If one already exists, drop the duplicate and return
        # the existing fleet row.
        fleet_rows = (
            await s.execute(
                select(OperatorFilter).where(
                    OperatorFilter.visibility == _VISIBILITY_FLEET,
                    OperatorFilter.id != row.id,
                )
            )
        ).scalars().all()
        canon_p = _pattern_canon(row.pattern)
        scope = row.scope or {}
        for f in fleet_rows:
            if _pattern_canon(f.pattern) == canon_p and _scope_equal(f.scope or {}, scope):
                await s.delete(row)
                await s.commit()
                return {
                    **_serialize_filter(f, viewer_id=ctx.user.id),
                    "deduped": True,
                    "note": (
                        "An equivalent fleet filter already exists. The private filter has "
                        "been removed and the existing fleet filter is returned."
                    ),
                }

        row.visibility = _VISIBILITY_FLEET
        await s.commit()
        await s.refresh(row)

    return {
        **_serialize_filter(row, viewer_id=ctx.user.id),
        "promoted": True,
        "note": (
            "Filter is now fleet-wide — visible to every operator. Only the original "
            "creator can hard-delete it via forget_filter."
        ),
    }


# ---------- Tool schemas (Claude tool-use shape) ----------


FILTERS_TOOLS: list[dict] = [
    {
        "name": "remember_filter",
        "description": (
            "Persist an operator-supplied rule to suppress a recurring noise pattern in "
            "FUTURE investigations. Call when the operator says things like 'ignore X', "
            "'treat Y as known noise', 'don't report Z', or 'make a note to skip that'. "
            "Filters created here are PRIVATE (only the calling operator sees them); use "
            "promote_filter to make one fleet-wide. The pattern is matched as a case-"
            "insensitive substring against log / error / alert text. Always pass a `reason` "
            "(the operator's WHY). Dedup: if an equivalent filter (your private OR any "
            "fleet) already exists, the existing one is returned with `deduped: true` and "
            "no new row is created."
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
                        "as a wildcard. Empty {} means 'any source'."
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
            "Return the noise-suppression filters that apply to the current investigation "
            "scope. Returns BOTH the calling operator's private filters AND every fleet-"
            "wide filter (regardless of original creator). ALWAYS call this at the START "
            "of any investigation that scopes to a device, group, or specific container — "
            "BEFORE you summarize errors, alerts, or noisy logs. Pass the BROADEST scope "
            "keys you know (usually just `device` and/or `group`). The server returns "
            "every filter whose scope is compatible with yours (including filters that pin "
            "EXTRA keys you didn't pass). For each returned filter, apply it PER LINE: "
            "drop the line iff (a) it contains the filter's `pattern` as a case-insensitive "
            "substring AND (b) the line's source is consistent with every key the filter "
            "pins. Acknowledge in a small footer like '_suppressed N lines matching M "
            "operator filters_' so the operator can spot over-silencing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "device": {"type": "string"},
                "container": {"type": "string"},
                "group": {"type": "string"},
                "signal": {"type": "string"},
            },
        },
    },
    {
        "name": "promote_filter",
        "description": (
            "Convert a private filter to fleet-wide visibility so every operator's future "
            "investigations apply it. Call when the operator says 'make that fleet-wide', "
            "'share that filter', 'promote it', or similar. ANY operator can promote. "
            "The original creator stays in the audit trail (user_id is preserved); only "
            "the creator can later forget a fleet filter via forget_filter. Dedup: if an "
            "equivalent fleet filter already exists, the source private filter is "
            "deleted and the existing fleet filter is returned with `deduped: true`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filter_id": {
                    "type": "string",
                    "description": "UUID of the filter to promote (from remember_filter or list_relevant_filters).",
                },
            },
            "required": ["filter_id"],
        },
    },
    {
        "name": "forget_filter",
        "description": (
            "Hard-delete a filter you own. Call when the operator says 'stop ignoring X', "
            "'remove that filter', 'I was wrong about Y'. You can only forget filters whose "
            "owner is the calling operator — i.e. your own private filters AND any fleet "
            "filters you originally created. Other operators' fleet filters are read-only "
            "to you; the tool returns an error pointing the operator at the creator."
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
    "promote_filter": promote_filter,
}
