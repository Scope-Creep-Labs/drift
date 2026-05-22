"""Web-terminal relay.

Two-sided WebSocket relay sitting between a logged-in operator's browser
and an edge agent. The agent has no inbound network (NAT, etc.); it
learns about a pending session via its next check-in (`pending_sessions`
list), then opens an outbound WS to the agent-side endpoint here.

State machine per session:

  POST /devices/{name}/terminal     row inserted, status="pending"
  ↓ (operator's WS connects first)
  attach_browser()                  state.browser_ws set, waiting on pair
  ↓ (agent's next check-in surfaces id, agent forks terminal-bridge.py)
  attach_agent()                    state.agent_ws set, pair signal fires
  ↓
  relay()                            row → "active", two byte-pump tasks
  ↓ (either side disconnects)
  cleanup()                          row → "closed", bytes counters flushed

In-memory only — a CP restart drops all in-flight sessions, which is
fine: operators reopen, agents pick up the new id next check-in. We
don't persist the WS objects.

Authz layers:
  - browser side: cookie session (UserContext) + deploy role + group
    access against device.group_id
  - agent side:   bearer token cross-checked against the device the
    session belongs to (so a compromised token for device A can't open
    a relay paired with device B)
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Header,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import session
from .models import Device, TerminalSession
from .security import verify_token
from ..users.deps import UserContext, get_current_user, require_role


log = logging.getLogger(__name__)


# Conservative limits — host shell access is sensitive enough that we'd
# rather error than over-allocate. Tune via env later if the load demands.
PENDING_TIMEOUT_SECONDS = 60          # agent must attach within 60s of POST
MAX_SESSION_SECONDS = 30 * 60         # hard cap per session (30min)
IDLE_TIMEOUT_SECONDS = 5 * 60         # both sides silent → close
MAX_CONCURRENT_SESSIONS = 16          # CP-wide cap


@dataclass
class _SessionState:
    """In-memory pairing state. The DB row carries durable bookkeeping
    (status, bytes, started_at) so a CP restart leaves an audit trail
    even though the relay itself is lost."""

    id: uuid.UUID
    device_id: uuid.UUID
    user_id: uuid.UUID
    paired: asyncio.Event = field(default_factory=asyncio.Event)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    browser_ws: WebSocket | None = None
    agent_ws: WebSocket | None = None
    bytes_b2a: int = 0
    bytes_a2b: int = 0


_sessions: dict[uuid.UUID, _SessionState] = {}
_sessions_lock = asyncio.Lock()


router = APIRouter(prefix="/api/deploy", tags=["deploy-terminal"])


async def get_db() -> AsyncIterator[AsyncSession]:
    async with session() as s:
        yield s


# ---------- session lifecycle ----------


async def _get_pending_for_device(db: AsyncSession, device_id: uuid.UUID) -> list[uuid.UUID]:
    """Pending session ids the device should know about. Filters out
    sessions whose agent WS is already attached on this CP, so the
    agent doesn't re-fork a bridge on every check-in while waiting
    for the browser to pair (each fork costs a docker layer for
    nsenter + /bin/login and spams logs)."""
    rows = (
        await db.execute(
            select(TerminalSession.id).where(
                TerminalSession.device_id == device_id,
                TerminalSession.status == "pending",
            )
        )
    ).scalars().all()
    return [
        sid for sid in rows
        if (state := _sessions.get(sid)) is None or state.agent_ws is None
    ]


async def _expire_session(session_id: uuid.UUID, reason: str) -> None:
    async with session() as s:
        row = await s.get(TerminalSession, session_id)
        if row is not None and row.status in ("pending", "active"):
            row.status = "expired" if reason == "timeout" else "closed"
            row.ended_at = datetime.now(timezone.utc)
            await s.commit()


async def _finalize_session(state: _SessionState, reason: str) -> None:
    """Mark the DB row closed and flush byte counters. Called from the
    cleanup path; safe to invoke twice (closed.set guards re-entry)."""
    if state.closed.is_set():
        return
    state.closed.set()
    async with session() as s:
        row = await s.get(TerminalSession, state.id)
        if row is not None:
            if row.status in ("pending", "active"):
                row.status = "expired" if reason == "timeout" else "closed"
            row.ended_at = datetime.now(timezone.utc)
            row.bytes_browser_to_agent = state.bytes_b2a
            row.bytes_agent_to_browser = state.bytes_a2b
            await s.commit()
    async with _sessions_lock:
        _sessions.pop(state.id, None)
    log.info("terminal session %s closed (%s)", state.id, reason)


async def _pump(
    src: WebSocket,
    dst: WebSocket,
    direction: str,  # "b2a" or "a2b"
    state: _SessionState,
) -> None:
    """Forward bytes/text frames between sides. Binary = pty stdio,
    text = JSON control (resize). Tracks bytes for audit. Returns when
    either side disconnects so the outer gather() can finalize."""
    try:
        while True:
            msg = await src.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if "bytes" in msg and msg["bytes"] is not None:
                payload = msg["bytes"]
                await dst.send_bytes(payload)
                if direction == "b2a":
                    state.bytes_b2a += len(payload)
                else:
                    state.bytes_a2b += len(payload)
            elif "text" in msg and msg["text"] is not None:
                # Forward as-is; the bridge interprets JSON control frames.
                await dst.send_text(msg["text"])
    except WebSocketDisconnect:
        return
    except Exception as e:  # noqa: BLE001
        log.warning("relay %s error: %s", direction, e)


# ---------- create session (browser-side POST) ----------


@router.post("/devices/{name}/terminal", status_code=status.HTTP_201_CREATED)
async def create_terminal_session(
    name: str,
    user: UserContext = Depends(require_role("deploy")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Reserve a session id. The browser then opens a WS to /ws/{id};
    the agent forks a bridge to the same id on its next check-in."""
    device = (
        await db.execute(select(Device).where(Device.name == name))
    ).scalar_one_or_none()
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"device '{name}' not found")
    # Group access: admins bypass, deploy users must own the device's
    # group. Same gate as deploy_revision/commission_device.
    if not user.is_admin:
        if device.group_id is None or not user.has_group(device.group_id):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"you don't have access to device '{name}'",
            )
    # Pre-flight: refuse if the device isn't currently online. Without
    # this the browser would create a session row, sit through the 60s
    # pairing timeout, then 4408. Better to fail fast with context so
    # the operator can decide whether to wait for the device to come
    # back or fix it. last_seen surfaces in the error so they can
    # judge how stale the offline-ness is.
    if device.status != "online":
        last_seen_str = device.last_seen.isoformat() if device.last_seen else "never"
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"device '{name}' is {device.status} (last seen: {last_seen_str}) — "
            "cannot open terminal until it checks in",
        )
    async with _sessions_lock:
        if len(_sessions) >= MAX_CONCURRENT_SESSIONS:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"too many concurrent terminal sessions ({MAX_CONCURRENT_SESSIONS})",
            )
        row = TerminalSession(
            device_id=device.id,
            user_id=user.id,
            status="pending",
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        _sessions[row.id] = _SessionState(
            id=row.id, device_id=device.id, user_id=user.id
        )
    log.info("terminal session %s created for device %s by user %s", row.id, name, user.username)
    return {"session_id": str(row.id)}


# ---------- browser-side WebSocket ----------


@router.websocket("/devices/{name}/terminal/ws/{session_id}")
async def browser_terminal_ws(
    websocket: WebSocket,
    name: str,
    session_id: uuid.UUID,
) -> None:
    # FastAPI's WebSocket route can't depend on get_current_user
    # (it raises HTTPException on no-cookie which the WS handshake
    # won't surface cleanly). Resolve auth manually here.
    from ..users.deps import resolve_user_from_cookie

    user = await resolve_user_from_cookie(websocket)
    if user is None:
        await websocket.close(code=4401)
        return
    if not (user.is_admin or user.role == "deploy"):
        await websocket.close(code=4403)
        return

    async with session() as db:
        device = (
            await db.execute(select(Device).where(Device.name == name))
        ).scalar_one_or_none()
        if device is None:
            await websocket.close(code=4404)
            return
        if not user.is_admin:
            if device.group_id is None or not user.has_group(device.group_id):
                await websocket.close(code=4403)
                return

    state = _sessions.get(session_id)
    if state is None or state.device_id != device.id:
        await websocket.close(code=4404)
        return
    if state.browser_ws is not None:
        # Reconnect after a network blip — replace the old WS so the
        # user doesn't have to reopen the terminal modal. Old WS gets
        # closed implicitly when the relay tasks exit on their next
        # send/recv against it.
        try:
            await state.browser_ws.close(code=4409)
        except Exception:  # noqa: BLE001
            pass

    await websocket.accept()
    state.browser_ws = websocket
    log.info("terminal %s browser attached (user=%s)", session_id, user.username)

    # Wait up to PENDING_TIMEOUT for the agent to attach. The agent
    # learns about the session via its next check-in (≤ POLL_INTERVAL
    # seconds, default 30s on the agent).
    try:
        await asyncio.wait_for(state.paired.wait(), timeout=PENDING_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        await websocket.send_text('{"type":"error","message":"agent did not attach within 60s"}')
        await websocket.close(code=4408)
        await _finalize_session(state, "timeout")
        return

    # Mark active in DB now that both sides are paired.
    async with session() as db:
        row = await db.get(TerminalSession, state.id)
        if row is not None:
            row.status = "active"
            await db.commit()

    # Run two byte-pumps in parallel; first to return triggers cleanup.
    try:
        done, pending = await asyncio.wait(
            [
                asyncio.create_task(_pump(websocket, state.agent_ws, "b2a", state)),  # type: ignore[arg-type]
                asyncio.create_task(_pump(state.agent_ws, websocket, "a2b", state)),  # type: ignore[arg-type]
            ],
            timeout=MAX_SESSION_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
        if state.agent_ws is not None:
            try:
                await state.agent_ws.close()
            except Exception:  # noqa: BLE001
                pass
        await _finalize_session(state, "browser_disconnect")


# ---------- agent-side WebSocket ----------


@router.websocket("/agent/terminal/ws/{session_id}")
async def agent_terminal_ws(
    websocket: WebSocket,
    session_id: uuid.UUID,
    authorization: str | None = Header(default=None),
) -> None:
    if not authorization or not authorization.lower().startswith("bearer "):
        await websocket.close(code=4401)
        return
    bearer = authorization.split(None, 1)[1].strip()

    # Cross-check: the bearer must match the device this session is
    # bound to. Same row lookup as a check-in but keyed on the session,
    # not the device name — the agent doesn't have to know its own row id.
    async with session() as db:
        row = await db.get(TerminalSession, session_id)
        if row is None:
            await websocket.close(code=4404)
            return
        device = await db.get(Device, row.device_id)
        if (
            device is None
            or device.bootstrap_token_hash is None
            or not verify_token(bearer, device.bootstrap_token_hash)
        ):
            await websocket.close(code=4401)
            return

    state = _sessions.get(session_id)
    if state is None:
        # CP must have restarted between session creation and agent attach.
        # The DB row is orphaned — mark expired so we don't show a phantom
        # pending row forever.
        await _expire_session(session_id, "orphaned")
        await websocket.close(code=4404)
        return

    await websocket.accept()
    state.agent_ws = websocket
    state.paired.set()
    log.info("terminal %s agent attached", session_id)

    # If the browser never paired (user closed the modal mid-create,
    # network blip, etc.), the agent is left attached with nothing on
    # the other side. Close after the pending timeout so the bridge
    # process on the device exits cleanly instead of leaking. The
    # browser-side handler sets state.closed on its happy path; we
    # also race against that here.
    if state.browser_ws is None:
        try:
            await asyncio.wait_for(state.closed.wait(), timeout=PENDING_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await websocket.close(code=4408)
            await _finalize_session(state, "no_browser_pair")
            return
    else:
        await state.closed.wait()