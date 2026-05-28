"""FastAPI dependency: bearer-token auth that resolves to a Device row.

Used by the agent surface (`/agent/check-in`). The token is matched against
`devices.bootstrap_token_hash`; lookup is by device_name carried in the
request body (so multiple devices can share an Authorization header path
without collision).
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import session
from .models import Device
from .naming import normalize_device_name
from .security import verify_token


async def _device_session() -> AsyncSession:  # pragma: no cover (FastAPI dep)
    async with session() as s:
        yield s


async def authenticate_device(
    device_name: str,
    bearer: str,
    db: AsyncSession,
) -> Device:
    # Normalize the agent-supplied name before lookup so a casing
    # mismatch between /etc/drift-deploy/env (set by install.sh from
    # whatever the operator typed) and the canonical DB form doesn't
    # cause spurious 401s. The DB column is always normalized.
    normalized = normalize_device_name(device_name)
    if not normalized:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid device or token")
    row = await db.execute(select(Device).where(Device.name == normalized))
    device = row.scalar_one_or_none()
    if device is None or device.bootstrap_token_hash is None:
        # Same error shape for "not found" and "no token" — don't leak which.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid device or token")
    if not verify_token(bearer, device.bootstrap_token_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid device or token")
    return device


def extract_bearer(authorization: Annotated[str | None, Header()] = None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Authorization: Bearer <token> required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return authorization.split(None, 1)[1].strip()
