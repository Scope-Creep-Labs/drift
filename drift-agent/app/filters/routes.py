"""REST endpoints backing the Filters sidebar surface.

Mirrors the agent-side `remember_filter` / `list_relevant_filters` /
`promote_filter` / `forget_filter` tools, but takes user input directly
from the SPA instead of going through the chat agent. Same auth model
as the rest of the SPA (cookie-based session); per-user scope is
enforced via `get_current_user`.

Listing returns BOTH the operator's private filters AND every fleet
filter, sorted by created_at desc — that's what the sidebar shows.
Promote can target any private filter the operator owns. Delete can
target any filter the operator owns (private or fleet they created).
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..deploy.models import OperatorFilter
from ..tools.filters import (
    _VISIBILITY_FLEET,
    _VISIBILITY_PRIVATE,
    _normalize_scope,
    _pattern_canon,
    _scope_equal,
    _serialize_filter,
)
from ..users.deps import UserContext, get_current_user, get_db


router = APIRouter(prefix="/api/filters", tags=["filters"])


# ---------- Schemas ----------


class FilterScope(BaseModel):
    device: Optional[str] = None
    container: Optional[str] = None
    group: Optional[str] = None
    signal: Optional[str] = None


class FilterCreate(BaseModel):
    pattern: str = Field(min_length=1, max_length=4000)
    scope: FilterScope = Field(default_factory=FilterScope)
    reason: str = Field(min_length=1, max_length=1000)


class FilterOut(BaseModel):
    id: str
    pattern: str
    scope: dict
    reason: str
    visibility: str
    owned_by_me: bool
    created_at: str
    last_applied_at: Optional[str]
    apply_count: int


# ---------- Endpoints ----------


@router.get("", response_model=list[FilterOut])
async def list_filters(
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[FilterOut]:
    """Filters visible to the operator: own private + every fleet row."""
    rows = (
        await db.execute(
            select(OperatorFilter)
            .where(
                or_(
                    (OperatorFilter.user_id == user.id)
                    & (OperatorFilter.visibility == _VISIBILITY_PRIVATE),
                    OperatorFilter.visibility == _VISIBILITY_FLEET,
                )
            )
            .order_by(OperatorFilter.created_at.desc())
        )
    ).scalars().all()
    return [FilterOut(**_serialize_filter(r, viewer_id=user.id)) for r in rows]


@router.post("", response_model=FilterOut, status_code=status.HTTP_201_CREATED)
async def create_filter(
    body: FilterCreate,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FilterOut:
    """Create a new private filter for the calling operator.

    Dedup mirrors `remember_filter`: if an equivalent filter is already
    visible (own private OR any fleet), the existing one is returned
    instead of inserting a duplicate (returned with 200 OK semantically
    — FastAPI still emits 201 because we declared it, which is fine
    since the existing row is being conceptually "created" from the
    operator's perspective)."""
    pattern = body.pattern.strip()
    scope = _normalize_scope(body.scope.model_dump(exclude_none=True))
    reason = body.reason.strip()

    visible = (
        await db.execute(
            select(OperatorFilter).where(
                or_(
                    (OperatorFilter.user_id == user.id)
                    & (OperatorFilter.visibility == _VISIBILITY_PRIVATE),
                    OperatorFilter.visibility == _VISIBILITY_FLEET,
                )
            )
        )
    ).scalars().all()
    canon_p = _pattern_canon(pattern)
    for r in visible:
        if _pattern_canon(r.pattern) == canon_p and _scope_equal(r.scope or {}, scope):
            return FilterOut(**_serialize_filter(r, viewer_id=user.id))

    row = OperatorFilter(
        user_id=user.id,
        pattern=pattern,
        scope=scope,
        reason=reason,
        visibility=_VISIBILITY_PRIVATE,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return FilterOut(**_serialize_filter(row, viewer_id=user.id))


@router.post("/{filter_id}/promote", response_model=FilterOut)
async def promote_filter(
    filter_id: str,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FilterOut:
    """Promote a private filter to fleet-wide.

    Any authenticated operator can promote ANY visible filter (own
    private — operators can't promote others' private filters because
    they can't see them). The original creator stays as `user_id` for
    audit. If an equivalent fleet filter already exists, the source
    row is deleted and the existing fleet row is returned."""
    try:
        fid = uuid.UUID(filter_id)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"filter_id '{filter_id}' is not a valid UUID")

    row = (
        await db.execute(select(OperatorFilter).where(OperatorFilter.id == fid))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"filter '{filter_id}' not found")

    if row.visibility == _VISIBILITY_FLEET:
        # Already fleet — nothing to do, return the row as-is so the
        # UI re-renders consistently.
        return FilterOut(**_serialize_filter(row, viewer_id=user.id))

    # Promoting requires the row be visible to the caller. Private
    # rows are only visible to their owner.
    if row.user_id != user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "cannot promote another operator's private filter",
        )

    # Dedup against existing fleet rows.
    fleet_rows = (
        await db.execute(
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
            await db.delete(row)
            await db.commit()
            return FilterOut(**_serialize_filter(f, viewer_id=user.id))

    row.visibility = _VISIBILITY_FLEET
    await db.commit()
    await db.refresh(row)
    return FilterOut(**_serialize_filter(row, viewer_id=user.id))


@router.delete("/{filter_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_filter(
    filter_id: str,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Hard-delete a filter the operator owns. Operators can only delete
    their OWN filters (private or fleet). To remove someone else's
    fleet filter, the original creator (or an admin via SQL) has to
    do it."""
    try:
        fid = uuid.UUID(filter_id)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"filter_id '{filter_id}' is not a valid UUID")

    row = (
        await db.execute(select(OperatorFilter).where(OperatorFilter.id == fid))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"filter '{filter_id}' not found")
    if row.user_id != user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "you can only delete filters you created",
        )
    await db.delete(row)
    await db.commit()
