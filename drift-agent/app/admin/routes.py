"""Admin-only update endpoints.

GET  /api/admin/updates         - latest poll snapshot (in-memory cache)
POST /api/admin/updates/check   - force a fresh poll, return the new snapshot
POST /api/admin/updates/apply   - pull + recreate drift-agent + drift-frontend
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..users.deps import require_role, UserContext
from . import updates

router = APIRouter(prefix="/api/admin/updates", tags=["admin"])


@router.get("")
async def get_updates(
    _admin: UserContext = Depends(require_role("admin")),
) -> dict:
    return updates.get_snapshot()


@router.post("/check")
async def force_check(
    _admin: UserContext = Depends(require_role("admin")),
) -> dict:
    return await updates.trigger_check()


@router.post("/apply")
async def apply_updates(
    _admin: UserContext = Depends(require_role("admin")),
) -> dict:
    # NOTE: drift-agent will recreate itself in this call. The HTTP
    # response races the container restart — the SPA should treat a
    # connection drop here as "likely succeeded; re-poll after the
    # SSE/WS reconnects".
    return await updates.apply_cp_updates()
