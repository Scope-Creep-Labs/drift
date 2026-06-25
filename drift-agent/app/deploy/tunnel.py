"""Tunnel relay — subdomain-routed HTTP/WS proxy through an edge agent.

An operator opens a tunnel against a device + port. The CP mints:

  - a random `subdomain_token`
  - a URL like `https://tunnel-<token>.<base_domain>/`
  - a row in `tunnel_sessions` (status=pending)

The edge agent learns about it via `pending_tunnels[]` on the next
check-in and forks `tunnel-bridge.py`, which opens an outbound WS to the
CP's agent-side endpoint here. Once attached, the in-memory bridge
state is paired with the session row.

When a request lands on the subdomain (routed in by Caddy + drift-
frontend nginx Host-based server block), the proxy middleware (in
`tunnel_proxy.py`) looks up the bridge state by subdomain_token and
multiplexes the request as a fresh channel through the WS. The bridge
opens a TCP connection to `localhost:<port>` and bridges raw bytes.

Auth layers:
  - Browser/operator side (mint/list/revoke): cookie session + deploy
    role + group access against the device.
  - Caddy ask-hook (`/api/internal/tunnel/check`): no auth — it's just a
    pre-flight that returns 200 if the subdomain has a live session.
    Used to gate Caddy's on-demand TLS so the cert authority isn't
    asked to issue for arbitrary names.
  - Agent WS attach (`/agent/tunnel/ws/{id}`): bearer = device bootstrap
    token, cross-checked against the session's device_id.
  - Subdomain HTTP/WS hits: anonymous; access is established by knowing
    the unguessable subdomain. The subdomain_token is treated as a
    capability — anyone with the URL can poke the upstream until the
    session expires or is revoked. (Same model as a magic link.)
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets as _secrets
import struct
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..users.deps import UserContext, get_current_user, require_role
from .db import session
from .models import Device, TunnelSession
from .security import verify_token


log = logging.getLogger(__name__)


# Pending → bridge-attached deadline. Mirrors terminal.py's 60s — the
# agent's poll cycle is 30s, so 60s is enough room for one missed tick.
PENDING_TIMEOUT_SECONDS = 60
# How long we wait inside the subdomain proxy for the bridge to attach
# before returning 503. Operators typically open the URL right after the
# modal returns; if the agent hasn't checked in yet, give it a beat
# rather than immediate-failing.
PROXY_ATTACH_WAIT_SECONDS = 35
# Per-WS frame size cap mirroring the edge bridge.
MAX_WS_FRAME = 2 ** 20  # 1 MiB
# Max simultaneous in-flight channels per tunnel. SPAs can pop a dozen
# parallel asset requests; 64 is comfortable headroom.
MAX_CHANNELS_PER_TUNNEL = 64


# ---------- in-memory bridge state ----------


@dataclass
class _BridgeState:
    """Per-tunnel-session live WS + multiplex channels. The DB row holds
    durable bookkeeping (subdomain, port, expires_at); this object holds
    everything that doesn't survive a CP restart."""

    id: uuid.UUID
    device_id: uuid.UUID
    user_id: uuid.UUID
    subdomain_token: str
    port: int
    expires_at: datetime
    paired: asyncio.Event = field(default_factory=asyncio.Event)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    bridge_ws: WebSocket | None = None
    # Outbound writes serialize through this lock — websockets isn't
    # safe under concurrent send() from multiple tasks.
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # channel_id → bytes queue. None pushed = EOF / channel close.
    channels: dict[int, "asyncio.Queue[bytes | None]"] = field(default_factory=dict)
    _next_channel: int = 1

    def alloc_channel(self) -> int | None:
        """Pick the next free channel id; return None if we're at cap."""
        if len(self.channels) >= MAX_CHANNELS_PER_TUNNEL:
            return None
        # 16-bit wraparound — 65535 ids; we skip any in active use so the
        # wrap is safe under steady-state load.
        for _ in range(65535):
            cid = self._next_channel
            self._next_channel = (self._next_channel % 65535) + 1
            if cid not in self.channels:
                self.channels[cid] = asyncio.Queue()
                return cid
        return None  # the table genuinely is full (shouldn't happen given cap above)

    async def send_open(self, channel_id: int) -> None:
        async with self.send_lock:
            await self.bridge_ws.send_text(  # type: ignore[union-attr]
                json.dumps({"type": "open", "channel": channel_id})
            )

    async def send_close(self, channel_id: int) -> None:
        if self.bridge_ws is None:
            return
        async with self.send_lock:
            try:
                await self.bridge_ws.send_text(
                    json.dumps({"type": "close", "channel": channel_id})
                )
            except Exception:  # noqa: BLE001
                pass

    async def send_data(self, channel_id: int, payload: bytes) -> None:
        # The 2-byte channel header is identical to what the edge bridge
        # speaks (struct.pack(">H", ...)). Keep these in lockstep.
        frame = struct.pack(">H", channel_id) + payload
        async with self.send_lock:
            await self.bridge_ws.send_bytes(frame)  # type: ignore[union-attr]

    def close_channel(self, channel_id: int) -> None:
        q = self.channels.pop(channel_id, None)
        if q is not None:
            # Non-blocking signal — readers awaiting an item see None and exit.
            q.put_nowait(None)


_bridges: dict[uuid.UUID, _BridgeState] = {}
_bridges_lock = asyncio.Lock()
# Reverse index: subdomain_token → session_id, populated on row insert.
# The subdomain proxy looks up by token (from the Host header), then
# fetches the bridge state by id.
_token_to_id: dict[str, uuid.UUID] = {}


def get_bridge_by_token(subdomain_token: str) -> _BridgeState | None:
    sid = _token_to_id.get(subdomain_token)
    if sid is None:
        return None
    return _bridges.get(sid)


def get_bridge_by_id(session_id: uuid.UUID) -> _BridgeState | None:
    return _bridges.get(session_id)


# ---------- session lifecycle helpers ----------


def _gen_subdomain_token() -> str:
    """32 hex chars (128 bits). MUST be all-lowercase, no separators —
    Caddy normalizes the SNI hostname to lowercase before calling the
    on-demand-TLS ask hook, so a mixed-case token would round-trip
    through Caddy as a different string than what's in `tunnel_sessions`
    and the ask hook would 404 (cert refused → browser TLS alert).
    token_hex gives lowercase a-f digits — safe by construction."""
    return _secrets.token_hex(16)


async def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _expire_row(session_id: uuid.UUID, terminal_status: str) -> None:
    async with session() as s:
        row = await s.get(TunnelSession, session_id)
        if row is not None and row.status in ("pending", "active"):
            row.status = terminal_status
            row.ended_at = datetime.now(timezone.utc)
            await s.commit()


async def _finalize(state: _BridgeState, reason: str, terminal_status: str = "closed") -> None:
    """Mark the row terminal + flush in-memory state. Safe to call twice."""
    if state.closed.is_set():
        return
    state.closed.set()
    async with session() as s:
        row = await s.get(TunnelSession, state.id)
        if row is not None and row.status in ("pending", "active"):
            row.status = terminal_status
            row.ended_at = datetime.now(timezone.utc)
            await s.commit()
    # Drain channels so any in-flight proxy requests unblock + see EOF.
    for cid in list(state.channels.keys()):
        state.close_channel(cid)
    async with _bridges_lock:
        _bridges.pop(state.id, None)
        _token_to_id.pop(state.subdomain_token, None)
    log.info("tunnel session %s closed (%s)", state.id, reason)


async def _get_pending_tunnels_for_device(
    db: AsyncSession, device_id: uuid.UUID
) -> list[dict]:
    """Pending tunnels the device should know about, filtering out any
    whose bridge is already attached on this CP (so the agent doesn't
    re-fork tunnel-bridge.py every check-in while we're waiting for the
    operator's browser). Mirrors terminal._get_pending_for_device."""
    rows = (
        await db.execute(
            select(TunnelSession.id, TunnelSession.port).where(
                TunnelSession.device_id == device_id,
                TunnelSession.status == "pending",
            )
        )
    ).all()
    out: list[dict] = []
    for sid, port in rows:
        state = _bridges.get(sid)
        if state is not None and state.bridge_ws is not None:
            continue
        out.append({"id": sid, "port": port})
    return out


# ---------- pydantic models ----------


class TunnelOpenRequest(BaseModel):
    port: int = Field(ge=1, le=65535)
    ttl_seconds: int | None = Field(default=None, ge=60, le=24 * 60 * 60)


class TunnelOut(BaseModel):
    id: uuid.UUID
    device_id: uuid.UUID
    port: int
    status: str
    url: str
    subdomain: str
    created_at: datetime
    expires_at: datetime
    ended_at: datetime | None = None


# ---------- router ----------


router = APIRouter(prefix="/api/deploy", tags=["deploy-tunnel"])


async def get_db() -> AsyncIterator[AsyncSession]:
    async with session() as s:
        yield s


def _tunnel_enabled_or_503() -> None:
    if not settings.tunnel_base_domain:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "tunnel feature is disabled — set TUNNEL_BASE_DOMAIN in the CP env",
        )


def _tunnel_url(subdomain_token: str) -> tuple[str, str]:
    """(full_url, host_only). host_only is what the operator sees on the
    SPA's address bar after they click `Open`."""
    host = f"tunnel-{subdomain_token}.{settings.tunnel_base_domain}"
    return f"https://{host}/", host


def _to_out(row: TunnelSession) -> TunnelOut:
    url, host = _tunnel_url(row.subdomain_token)
    return TunnelOut(
        id=row.id,
        device_id=row.device_id,
        port=row.port,
        status=row.status,
        url=url,
        subdomain=host,
        created_at=row.created_at,
        expires_at=row.expires_at,
        ended_at=row.ended_at,
    )


@router.post(
    "/devices/{name}/tunnel/open",
    response_model=TunnelOut,
    status_code=status.HTTP_201_CREATED,
)
async def open_tunnel(
    name: str,
    body: TunnelOpenRequest,
    user: UserContext = Depends(require_role("deploy")),
    db: AsyncSession = Depends(get_db),
) -> TunnelOut:
    """Mint a new tunnel session for the device. Returns the URL the
    operator should open. The agent forks tunnel-bridge.py within
    `PENDING_TIMEOUT_SECONDS` of this call — subsequent subdomain hits
    wait briefly for the bridge to attach before returning 503."""
    _tunnel_enabled_or_503()
    device = (
        await db.execute(select(Device).where(Device.name == name))
    ).scalar_one_or_none()
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"device '{name}' not found")
    if not user.is_admin:
        if device.group_id is None or not user.has_group(device.group_id):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"you don't have access to device '{name}'",
            )
    if device.status != "online":
        last = device.last_seen.isoformat() if device.last_seen else "never"
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"device '{name}' is {device.status} (last seen: {last}) — "
            "cannot open tunnel until it checks in",
        )
    async with _bridges_lock:
        if len(_bridges) >= settings.tunnel_max_concurrent:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"too many concurrent tunnels ({settings.tunnel_max_concurrent})",
            )
        ttl = body.ttl_seconds or settings.tunnel_default_ttl_seconds
        token = _gen_subdomain_token()
        now = datetime.now(timezone.utc)
        row = TunnelSession(
            device_id=device.id,
            user_id=user.id,
            subdomain_token=token,
            port=body.port,
            status="pending",
            expires_at=now + timedelta(seconds=ttl),
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        state = _BridgeState(
            id=row.id,
            device_id=device.id,
            user_id=user.id,
            subdomain_token=token,
            port=body.port,
            expires_at=row.expires_at,
        )
        _bridges[row.id] = state
        _token_to_id[token] = row.id
    log.info(
        "tunnel %s opened device=%s port=%d user=%s ttl=%ds",
        row.id, name, body.port, user.username, ttl,
    )
    return _to_out(row)


@router.get("/devices/{name}/tunnels", response_model=list[TunnelOut])
async def list_tunnels(
    name: str,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[TunnelOut]:
    """List active (pending+active) tunnel sessions for a device. Filters
    by group access — a user without access to the device gets an empty
    list, same as for other deploy GETs."""
    device = (
        await db.execute(select(Device).where(Device.name == name))
    ).scalar_one_or_none()
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"device '{name}' not found")
    if not user.is_admin:
        if device.group_id is None or not user.has_group(device.group_id):
            return []
    rows = (
        await db.execute(
            select(TunnelSession).where(
                TunnelSession.device_id == device.id,
                TunnelSession.status.in_(("pending", "active")),
            ).order_by(TunnelSession.created_at.desc())
        )
    ).scalars().all()
    return [_to_out(r) for r in rows]


@router.delete(
    "/tunnels/{tunnel_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_tunnel(
    tunnel_id: uuid.UUID,
    user: UserContext = Depends(require_role("deploy")),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Revoke a tunnel — terminates the bridge WS and marks the row.
    Only the user who opened it (or an admin) can revoke; the audit log
    is the row's status transition."""
    row = await db.get(TunnelSession, tunnel_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"tunnel '{tunnel_id}' not found")
    if not user.is_admin and row.user_id != user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "only the user who opened this tunnel can revoke it",
        )
    state = _bridges.get(tunnel_id)
    if state is not None:
        await _finalize(state, "revoked", terminal_status="revoked")
        # Force the WS shut so the bridge process on the device exits;
        # this finally clause races against _finalize's own bridge_ws.close()
        # in some paths so guard with try/except.
        if state.bridge_ws is not None:
            try:
                await state.bridge_ws.close()
            except Exception:  # noqa: BLE001
                pass
    else:
        # Bridge never attached (still pending) or already terminal.
        await _expire_row(tunnel_id, "revoked")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------- Caddy on-demand TLS ask hook ----------


@router.get("/internal/tunnel/check", include_in_schema=False)
async def caddy_ask_hook(
    domain: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Caddy's on-demand-TLS `ask` callback. Returns 200 if `domain` has a
    live tunnel session, else 404. Caddy ONLY asks the upstream CA for a
    cert if this returns 200 — so an attacker hitting tunnel-junk.dabba…
    can't trick us into issuing thousands of throwaway certs."""
    base = settings.tunnel_base_domain
    if not base:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    suffix = "." + base
    if not domain.endswith(suffix):
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    label = domain[: -len(suffix)]
    if not label.startswith("tunnel-"):
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    token = label[len("tunnel-"):]
    # In-memory first (covers active sessions); fall back to DB so a
    # CP restart doesn't immediately tank in-flight tunnels mid-cert.
    if token in _token_to_id:
        return Response(status_code=status.HTTP_200_OK)
    row = (
        await db.execute(
            select(TunnelSession.id).where(
                TunnelSession.subdomain_token == token,
                TunnelSession.status.in_(("pending", "active")),
            )
        )
    ).first()
    if row is None:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    return Response(status_code=status.HTTP_200_OK)


# ---------- agent-side WebSocket ----------


@router.websocket("/agent/tunnel/ws/{session_id}")
async def agent_tunnel_ws(
    websocket: WebSocket,
    session_id: uuid.UUID,
    authorization: str | None = Header(default=None),
) -> None:
    """The edge agent's tunnel-bridge.py connects here. Cross-checks the
    bearer against the device the session belongs to (same pattern as
    /agent/terminal/ws/...), then pairs the WS to the in-memory bridge
    state. Bytes flow either direction; control frames open/close
    channels. The bridge advertises its port in the first text frame
    (`{"type":"ready","port":N}`); we accept and pair on that signal."""
    if not authorization or not authorization.lower().startswith("bearer "):
        await websocket.close(code=4401)
        return
    bearer = authorization.split(None, 1)[1].strip()

    async with session() as db:
        row = await db.get(TunnelSession, session_id)
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

    state = _bridges.get(session_id)
    if state is None:
        # Row exists but no in-memory state — CP restarted between mint
        # and bridge attach. Mark expired and reject so the bridge exits.
        await _expire_row(session_id, "expired")
        await websocket.close(code=4404)
        return

    await websocket.accept()
    state.bridge_ws = websocket
    log.info("tunnel %s bridge attached", session_id)

    # Receive loop: distribute binary frames to per-channel queues, react
    # to control frames. The first ready frame promotes us from pending
    # to active.
    promoted = False
    try:
        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if "bytes" in msg and msg["bytes"] is not None:
                data = msg["bytes"]
                if len(data) < 2:
                    continue
                cid = struct.unpack(">H", data[:2])[0]
                payload = data[2:]
                q = state.channels.get(cid)
                if q is None:
                    # Channel was already torn down on our side, or the
                    # bridge wrote to a channel we never opened (buggy
                    # bridge). Drop silently — clients won't see this.
                    continue
                await q.put(payload)
                continue
            if "text" in msg and msg["text"] is not None:
                try:
                    ctl = json.loads(msg["text"])
                except json.JSONDecodeError:
                    continue
                kind = ctl.get("type")
                if kind == "ready":
                    if not promoted:
                        promoted = True
                        state.paired.set()
                        async with session() as db2:
                            r = await db2.get(TunnelSession, state.id)
                            if r is not None and r.status == "pending":
                                r.status = "active"
                                await db2.commit()
                    continue
                if kind == "close":
                    cid_ctl = ctl.get("channel")
                    if isinstance(cid_ctl, int):
                        state.close_channel(cid_ctl)
                    continue
    except WebSocketDisconnect:
        pass
    finally:
        await _finalize(state, "bridge_disconnect")


# ---------- expiry sweep ----------


_sweep_task: asyncio.Task | None = None


async def _sweep_loop() -> None:
    """Background sweep: reap tunnels whose expires_at has passed. The
    PENDING_TIMEOUT case is handled inline in the proxy (timeout when
    waiting for `paired`); this catches the TTL case so a tab the
    operator left open doesn't keep the bridge alive forever."""
    while True:
        try:
            await asyncio.sleep(60)
            now = datetime.now(timezone.utc)
            doomed: list[_BridgeState] = []
            for state in list(_bridges.values()):
                if state.expires_at <= now:
                    doomed.append(state)
            for state in doomed:
                await _finalize(state, "ttl_expired", terminal_status="expired")
                if state.bridge_ws is not None:
                    try:
                        await state.bridge_ws.close()
                    except Exception:  # noqa: BLE001
                        pass
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            log.warning("tunnel sweep tick failed: %s", e)


def start_sweep() -> None:
    global _sweep_task
    if _sweep_task is None or _sweep_task.done():
        loop = asyncio.get_event_loop()
        _sweep_task = loop.create_task(_sweep_loop())


async def stop_sweep() -> None:
    global _sweep_task
    if _sweep_task is not None:
        _sweep_task.cancel()
        try:
            await _sweep_task
        except asyncio.CancelledError:
            pass
        _sweep_task = None
