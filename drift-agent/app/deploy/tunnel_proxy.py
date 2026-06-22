"""Subdomain proxy middleware for the tunnel feature.

Lives at the ASGI layer (not FastAPI route) because the matching is by
`Host` header, not path: any request whose Host is
`tunnel-<token>.<tunnel_base_domain>` gets routed through the multiplex
bridge to the device's `localhost:<port>`, regardless of path.

Two scope types are handled:

  - `http`: serialize request via h11, send as raw bytes on a fresh
    channel, parse response via h11 from the bytes streaming back,
    re-emit as ASGI response.
  - `websocket`: accept the upgrade on this side, open a channel on the
    bridge, send the synthesized WS handshake bytes through, then
    bidirectionally pump frames. The bridge is byte-only — it doesn't
    understand WS framing — so we marshal each direction as raw bytes
    and let the upstream app on the device handle frame parsing on
    its socket.

Everything else (mint endpoints, agent WS, ask hook) lives in tunnel.py.
"""
from __future__ import annotations

import asyncio
import logging
import struct
from typing import Awaitable, Callable

import h11

from ..config import settings
from .tunnel import (
    PROXY_ATTACH_WAIT_SECONDS,
    _BridgeState,
    get_bridge_by_token,
)


log = logging.getLogger(__name__)


# Pulled from the request's per-channel queue; controls back-pressure
# for response body streaming. Two strategies bound this: h11 needs
# enough to parse a status+headers chunk fast (any TCP chunk size works);
# for body forwarding we just keep draining.
CHANNEL_READ_TIMEOUT_SECONDS = 30


def _host_token(headers: list[tuple[bytes, bytes]]) -> str | None:
    """Extract the subdomain token from the Host header. Returns None if
    the host doesn't look like `tunnel-<token>.<base>`."""
    base = settings.tunnel_base_domain
    if not base:
        return None
    suffix = b"." + base.encode("ascii")
    for name, value in headers:
        if name.lower() != b"host":
            continue
        # Strip an optional :port suffix.
        host_only = value.split(b":", 1)[0]
        if not host_only.endswith(suffix):
            return None
        label = host_only[: -len(suffix)]
        if not label.startswith(b"tunnel-"):
            return None
        token = label[len("tunnel-"):]
        try:
            return token.decode("ascii")
        except UnicodeDecodeError:
            return None
    return None


class TunnelProxyMiddleware:
    """ASGI middleware. If the incoming request is for a tunnel
    subdomain, proxy it; otherwise hand off to the wrapped app."""

    def __init__(self, app: Callable):
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        token = _host_token(scope.get("headers", []))
        if token is None:
            await self.app(scope, receive, send)
            return
        state = get_bridge_by_token(token)
        if state is None:
            await _http_404(send, b"tunnel not found or expired")
            return
        # Block briefly for the bridge to attach if the operator opened the
        # URL before the agent's next check-in surfaced the pending session.
        if not state.paired.is_set():
            try:
                await asyncio.wait_for(state.paired.wait(), timeout=PROXY_ATTACH_WAIT_SECONDS)
            except asyncio.TimeoutError:
                await _http_503(send, b"device tunnel agent did not attach in time")
                return
        if state.bridge_ws is None or state.closed.is_set():
            await _http_502(send, b"tunnel bridge unavailable")
            return
        if scope["type"] == "http":
            await _proxy_http(state, scope, receive, send)
        else:
            await _proxy_websocket(state, scope, receive, send)


# ---------- HTTP proxy ----------


async def _http_503(send: Callable, message: bytes) -> None:
    await _simple_response(send, 503, message, b"text/plain; charset=utf-8")


async def _http_502(send: Callable, message: bytes) -> None:
    await _simple_response(send, 502, message, b"text/plain; charset=utf-8")


async def _http_404(send: Callable, message: bytes) -> None:
    await _simple_response(send, 404, message, b"text/plain; charset=utf-8")


async def _simple_response(
    send: Callable, status_code: int, body: bytes, content_type: bytes
) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", content_type),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


def _build_request_headers(scope: dict) -> list[tuple[bytes, bytes]]:
    """Translate the ASGI request headers into the form h11 expects.
    Override Host so the upstream app sees the bridge's loopback name
    (most apps don't care; some — Grafana with `serve_from_sub_path` —
    inspect Host for redirect URLs). Drop Connection: upgrade hop-by-
    hop headers; h11 manages those itself."""
    HOP_BY_HOP = {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
        b"content-length",  # h11 sets this from Data
    }
    out: list[tuple[bytes, bytes]] = []
    for name, value in scope["headers"]:
        if name.lower() in HOP_BY_HOP:
            continue
        if name.lower() == b"host":
            # Replace with localhost so a redirect-emitting app stays
            # within the subdomain (caller's browser will follow back
            # through us). The original Host is also available via
            # X-Forwarded-Host below.
            out.append((b"host", b"localhost"))
            continue
        out.append((name, value))
    # Stamp the original host through X-Forwarded-* so an upstream that
    # honors them (rare for debug UIs but harmless) renders correct URLs.
    for name, value in scope["headers"]:
        if name.lower() == b"host":
            out.append((b"x-forwarded-host", value))
            break
    out.append((b"x-forwarded-proto", b"https"))
    return out


async def _read_request_body(receive: Callable) -> AsyncIterator_of_bytes:  # type: ignore[valid-type]
    """Yield request body chunks until ASGI signals more_body=False."""
    more = True
    while more:
        message = await receive()
        if message["type"] != "http.request":
            continue
        body = message.get("body", b"")
        more = message.get("more_body", False)
        if body:
            yield body
        elif not more:
            return


# Python typing helper since `AsyncIterator[bytes]` would import from typing;
# keep this module's top compact.
from typing import AsyncIterator as AsyncIterator_of_bytes  # noqa: E402  -- after-use is intentional


async def _proxy_http(
    state: _BridgeState, scope: dict, receive: Callable, send: Callable
) -> None:
    channel_id = state.alloc_channel()
    if channel_id is None:
        await _http_503(send, b"too many in-flight requests on this tunnel")
        return
    q = state.channels[channel_id]
    try:
        await state.send_open(channel_id)

        # Build & send the HTTP/1.1 request via h11. http_version is fixed
        # to "1.1" because Caddy → nginx → drift-agent is already 1.1 by
        # the time we're here; the upstream app on the device is dialed
        # via raw TCP so HTTP/1.1 is the lingua franca.
        h11_client = h11.Connection(our_role=h11.CLIENT)
        target = scope["path"].encode("utf-8")
        if scope.get("query_string"):
            target += b"?" + scope["query_string"]
        request = h11.Request(
            method=scope["method"].encode("ascii"),
            target=target,
            headers=_build_request_headers(scope),
            http_version=b"1.1",
        )
        # Send request head, then body chunks, then EndOfMessage.
        head_bytes = h11_client.send(request)
        if head_bytes:
            await state.send_data(channel_id, head_bytes)
        async for chunk in _read_request_body(receive):
            data_bytes = h11_client.send(h11.Data(data=chunk))
            if data_bytes:
                await state.send_data(channel_id, data_bytes)
        end_bytes = h11_client.send(h11.EndOfMessage())
        if end_bytes:
            await state.send_data(channel_id, end_bytes)

        # Read response via h11 from bytes streaming back over the channel.
        h11_server = h11.Connection(our_role=h11.SERVER)
        response_started = False
        keep_running = True
        while keep_running:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=CHANNEL_READ_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                if not response_started:
                    await _http_502(send, b"upstream tunnel timed out")
                # If we already started streaming and the upstream went
                # silent, we have no choice but to terminate the response.
                return
            if chunk is None:
                # EOF — signal h11 by giving it empty bytes once.
                h11_server.receive_data(b"")
            else:
                h11_server.receive_data(chunk)
            while True:
                try:
                    event = h11_server.next_event()
                except h11.RemoteProtocolError as e:
                    log.warning("tunnel %s upstream gave malformed HTTP: %s", state.id, e)
                    if not response_started:
                        await _http_502(send, b"upstream sent malformed HTTP")
                    return
                if event is h11.NEED_DATA:
                    break
                if isinstance(event, h11.Response):
                    headers = _strip_hop_by_hop(event.headers)
                    await send(
                        {
                            "type": "http.response.start",
                            "status": event.status_code,
                            "headers": headers,
                        }
                    )
                    response_started = True
                    continue
                if isinstance(event, h11.Data):
                    if event.data:
                        await send(
                            {
                                "type": "http.response.body",
                                "body": bytes(event.data),
                                "more_body": True,
                            }
                        )
                    continue
                if isinstance(event, h11.EndOfMessage):
                    await send(
                        {"type": "http.response.body", "body": b"", "more_body": False}
                    )
                    keep_running = False
                    break
                if isinstance(event, h11.ConnectionClosed):
                    keep_running = False
                    break
    finally:
        # Tell the bridge to release the upstream socket. If the bridge
        # already saw EOF and sent close, this is a no-op.
        await state.send_close(channel_id)
        state.close_channel(channel_id)


def _strip_hop_by_hop(
    headers: list[tuple[bytes, bytes]]
) -> list[tuple[bytes, bytes]]:
    HOP_BY_HOP = {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
    }
    return [(n, v) for (n, v) in headers if n.lower() not in HOP_BY_HOP]


# ---------- WebSocket proxy ----------


async def _proxy_websocket(
    state: _BridgeState, scope: dict, receive: Callable, send: Callable
) -> None:
    """The tunnel is a raw TCP byte stream on the bridge side, so we
    can't transparently forward an upgraded ASGI WS. Instead:

      1. Open a channel on the bridge.
      2. Hand-craft the HTTP/1.1 GET … Upgrade request and send it as
         raw bytes so the upstream app handshakes against the bridge's
         TCP socket.
      3. Read bytes back through the channel until we see the upstream's
         "HTTP/1.1 101 Switching Protocols" response (using h11).
      4. Accept the ASGI WebSocket on our side with the same subprotocol
         the upstream chose (if any).
      5. Pump messages bidirectionally — text/binary frames in ASGI
         become raw WS frames in `wsproto` outgoing bytes on the bridge
         side, and incoming bytes get parsed back into text/binary.

    The decoder is wsproto (already a transitive dep of nothing here so
    we'd have to add it). For v0.1.59 the simpler shape is: do the
    upgrade as above, then refuse to forward — return a friendly error
    asking the operator to update to the next version once we ship
    full WS support. Most "I want to poke at a UI" use cases work
    without in-tunnel WebSockets; declaring this gracefully is better
    than half-baked frame forwarding.
    """
    await send({"type": "websocket.close", "code": 1011, "reason": "tunnel WS forwarding not yet supported"})
    log.info(
        "tunnel %s received WS upgrade for %s — refusing (not yet supported)",
        state.id, scope.get("path"),
    )
