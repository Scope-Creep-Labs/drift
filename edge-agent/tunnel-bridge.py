#!/usr/bin/env python3
"""Drift tunnel bridge.

Spawned by drift-deploy-agent.sh when a `pending_tunnels[]` entry shows
up in the check-in response. Opens a WebSocket to the CP's agent-side
tunnel endpoint, then forwards each incoming TCP request frame to
localhost:<port> on the device. Bytes flow:

    operator browser
      ↓ Caddy (tunnel-<tok>.dabba…)
      ↓ drift-frontend nginx (Host-based proxy)
      ↓ drift-agent subdomain router
      ↓ multiplexed WS (this script)
      ↓ TCP to localhost:<port> on the device
      ↓ the operator's app (debug UI, Grafana, etc.)

Sibling to terminal-bridge.py rather than a mode of it — the protocols
are entirely different (pty vs multiplexed TCP) and keeping them
separate makes both easier to read.

Multiplex protocol (frames on the WS):

  Text  (JSON):   {"type":"ready","port":N}     bridge → CP, once on connect
                  {"type":"open","channel":N}    CP → bridge, dial localhost:port
                  {"type":"close","channel":N}   either side, tear channel down
  Binary:         [2 bytes channel_id big-endian][payload bytes]
                  Either direction. Channel_id maps to a TCP socket on the
                  bridge side; payload is raw bytes (HTTP wire format etc.).

One drift-agent process can multiplex many concurrent HTTP requests over
a single tunnel session by assigning fresh channel ids; the bridge opens
a fresh TCP socket per channel and reaps it on close.

Requires `python3` + `py3-websockets`. Inherits the same image baseline
as terminal-bridge.py — no extra deps to ship.
"""
from __future__ import annotations

import asyncio
import json
import struct
import sys

import websockets
from websockets.exceptions import ConnectionClosed


# Per-frame read budget. HTTP request/response bodies are streamed in
# chunks; this is just the WS frame size cap, not a per-request cap.
MAX_WS_FRAME = 2 ** 20  # 1 MiB
READ_CHUNK = 64 * 1024


class _Channel:
    """One in-flight TCP socket on the bridge side, paired with a CP
    channel_id. Reader pump runs as a task; writer is driven directly
    from the WS receive loop."""

    __slots__ = ("id", "reader", "writer", "pump_task", "closed")

    def __init__(self, id_: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.id = id_
        self.reader = reader
        self.writer = writer
        self.pump_task: asyncio.Task | None = None
        self.closed = False

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.pump_task is not None and not self.pump_task.done():
            self.pump_task.cancel()
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


async def _channel_pump_to_ws(ch: _Channel, ws) -> None:
    """Read from the TCP socket and forward as channel-prefixed binary
    WS frames. Exits when the socket closes or the WS errors; the
    matching {"type":"close"} control is sent by the caller."""
    header = struct.pack(">H", ch.id)
    try:
        while True:
            try:
                data = await ch.reader.read(READ_CHUNK)
            except (ConnectionResetError, BrokenPipeError, OSError):
                return
            if not data:
                return  # EOF from upstream
            try:
                await ws.send(header + data)
            except ConnectionClosed:
                return
    finally:
        # Tell the CP the channel is done. The CP relies on this to
        # finish its HTTP response (close-after-EOF). Best-effort —
        # if the WS is already gone, the CP will tear down via its
        # own disconnect path.
        try:
            await ws.send(json.dumps({"type": "close", "channel": ch.id}))
        except Exception:  # noqa: BLE001
            pass


async def run(ws_url: str, bearer: str, port: int) -> None:
    headers = {"Authorization": f"Bearer {bearer}"}
    # Same websockets-version detection as terminal-bridge.py — the
    # 12→13 kwarg rename silently breaks on 12 if you guess wrong.
    ws_major = int(websockets.__version__.split(".", 1)[0])
    header_kwarg = "additional_headers" if ws_major >= 13 else "extra_headers"
    connect_kwargs = {
        header_kwarg: headers,
        "max_size": MAX_WS_FRAME,
        "ping_interval": None,
        "ping_timeout": None,
        "close_timeout": 10,
    }
    sys.stderr.write(f"tunnel-bridge: connecting to {ws_url} (target port {port})\n")
    try:
        async with websockets.connect(ws_url, **connect_kwargs) as ws:
            # Tell the CP we're ready + advertise the port we'll dial.
            # The CP uses this as the "agent attached" signal — same
            # semantic as terminal_bridge's first frame.
            await ws.send(json.dumps({"type": "ready", "port": port}))
            sys.stderr.write("tunnel-bridge: ws connected, ready\n")

            channels: dict[int, _Channel] = {}

            async def open_channel(channel_id: int) -> None:
                if channel_id in channels:
                    sys.stderr.write(
                        f"tunnel-bridge: duplicate open for channel {channel_id}, ignoring\n"
                    )
                    return
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                except (ConnectionRefusedError, OSError) as e:
                    sys.stderr.write(
                        f"tunnel-bridge: dial localhost:{port} failed for channel "
                        f"{channel_id}: {e}\n"
                    )
                    # Tell the CP immediately; it'll surface a 502 to the
                    # operator's browser instead of hanging.
                    try:
                        await ws.send(
                            json.dumps({"type": "close", "channel": channel_id})
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    return
                ch = _Channel(channel_id, reader, writer)
                channels[channel_id] = ch
                ch.pump_task = asyncio.create_task(_channel_pump_to_ws(ch, ws))

            async def close_channel(channel_id: int) -> None:
                ch = channels.pop(channel_id, None)
                if ch is not None:
                    await ch.close()

            try:
                async for msg in ws:
                    if isinstance(msg, bytes):
                        if len(msg) < 2:
                            continue
                        channel_id = struct.unpack(">H", msg[:2])[0]
                        payload = msg[2:]
                        ch = channels.get(channel_id)
                        if ch is None or ch.closed:
                            # CP wrote to a channel we already tore down
                            # (raced with EOF). Drop silently — CP will
                            # see our matching close frame.
                            continue
                        try:
                            ch.writer.write(payload)
                            await ch.writer.drain()
                        except (ConnectionResetError, BrokenPipeError, OSError):
                            await close_channel(channel_id)
                        continue

                    # Text frame — JSON control.
                    try:
                        ctl = json.loads(msg)
                    except json.JSONDecodeError:
                        continue
                    kind = ctl.get("type")
                    cid = ctl.get("channel")
                    if not isinstance(cid, int):
                        continue
                    if kind == "open":
                        await open_channel(cid)
                    elif kind == "close":
                        await close_channel(cid)
            finally:
                # WS gone — tear down every TCP socket so we don't leak
                # half-open connections to the operator's app.
                for ch in list(channels.values()):
                    await ch.close()
                sys.stderr.write("tunnel-bridge: ws closed, all channels reaped\n")
    except Exception as e:  # noqa: BLE001 — surface to docker logs
        sys.stderr.write(f"tunnel-bridge: {e}\n")
        sys.exit(1)


def main() -> None:
    if len(sys.argv) != 4:
        sys.stderr.write("usage: tunnel-bridge.py <ws_url> <bearer> <target_port>\n")
        sys.exit(2)
    try:
        port = int(sys.argv[3])
    except ValueError:
        sys.stderr.write(f"tunnel-bridge: invalid port {sys.argv[3]!r}\n")
        sys.exit(2)
    if not (1 <= port <= 65535):
        sys.stderr.write(f"tunnel-bridge: port out of range: {port}\n")
        sys.exit(2)
    asyncio.run(run(sys.argv[1], sys.argv[2], port))


if __name__ == "__main__":
    main()
