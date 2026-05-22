#!/usr/bin/env python3
"""Drift terminal bridge.

Spawned by drift-deploy-agent.sh when a `pending_sessions[]` entry shows
up in the check-in response. Opens a WebSocket to the CP's agent-side
terminal endpoint, allocates a pty, and execs `/bin/login` inside the
host's namespaces (via nsenter on PID 1). Bytes flow:

    browser ↔ CP relay ↔ this script's WS ↔ pty master ↔ /bin/login

The bash agent is single-threaded; each session gets its own Python
process so a slow tty doesn't block reconciles.

Control frames from the browser arrive as JSON text messages and are
distinguished from terminal bytes (which are framed as binary). Today
we only handle `{"type":"resize","cols":N,"rows":M}` to TIOCSWINSZ the
pty.

Requires `python3` + `py3-websockets` + `util-linux` (for nsenter).
The agent container's Dockerfile installs all three; the host kernel
must support PID/mount/uts namespace entry (every modern Linux does).
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import struct
import sys
import termios

import websockets
from websockets.exceptions import ConnectionClosed


# Sized for a single screen of output, balances throughput and latency.
READ_CHUNK = 4096


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    """Apply rows/cols via TIOCSWINSZ. Browser-driven resizes route here
    so the remote `bash` reflows on `$COLUMNS` changes (less, vim, etc.
    are unusable without this). Values are int16, capped to defend
    against malformed control frames."""
    rows = max(1, min(rows, 32767))
    cols = max(1, min(cols, 32767))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _spawn_login() -> tuple[int, int]:
    """fork() + pty.openpty() so the child has a controlling tty. The
    child enters the host's namespaces and execs /bin/login — PAM does
    the rest. Returns (pid, master_fd)."""
    pid, fd = pty.fork()
    if pid == 0:
        # Child: enter host namespaces, then exec login. Mount + pid +
        # uts + ipc cover everything login needs to see the host's
        # /etc/passwd, /etc/shadow, /proc, and hostname.
        os.execvp(
            "nsenter",
            [
                "nsenter",
                "--target", "1",
                "--mount",
                "--pid",
                "--uts",
                "--ipc",
                "--",
                "/bin/login",
            ],
        )
        # exec only returns on failure; tell the parent something
        # specific so the WS surfaces a real error rather than a
        # confusing EOF.
        sys.stderr.write("exec nsenter/login failed\n")
        os._exit(127)
    return pid, fd


async def _pty_to_ws(fd: int, ws) -> None:
    """Read from the pty master and forward as binary WS frames. Uses
    an executor for the blocking read because asyncio doesn't have a
    portable read-from-pty primitive."""
    loop = asyncio.get_running_loop()
    while True:
        try:
            data = await loop.run_in_executor(None, os.read, fd, READ_CHUNK)
        except OSError:
            return  # pty closed (login exited)
        if not data:
            return
        try:
            await ws.send(data)
        except ConnectionClosed:
            return


async def _ws_to_pty(fd: int, ws) -> None:
    """Receive WS frames and write to the pty master. Binary frames are
    raw stdin; text frames are JSON control messages."""
    async for msg in ws:
        if isinstance(msg, bytes):
            try:
                os.write(fd, msg)
            except OSError:
                return
            continue
        # Text frame — JSON control. Today only resize; ignore others
        # quietly so we can extend the protocol without breaking older
        # clients.
        try:
            payload = json.loads(msg)
        except json.JSONDecodeError:
            continue
        if payload.get("type") == "resize":
            try:
                _set_winsize(fd, int(payload["rows"]), int(payload["cols"]))
            except (KeyError, TypeError, ValueError):
                pass


async def run(ws_url: str, bearer: str) -> None:
    # Headers carry the agent's bootstrap token — same credential it
    # uses for /agent/check-in. The CP cross-checks it against the
    # session row's device_id before wiring the relay.
    # The kwarg name for custom request headers changed between
    # websockets 12.x (`extra_headers`) and 13.x (`additional_headers`)
    # and v12 silently forwards unknown kwargs to asyncio rather than
    # raising — so detect via version, don't try/except.
    headers = {"Authorization": f"Bearer {bearer}"}
    ws_major = int(websockets.__version__.split(".", 1)[0])
    header_kwarg = "additional_headers" if ws_major >= 13 else "extra_headers"
    # ping_interval=None: the proxy chain (Caddy → nginx → uvicorn) does
    # not reliably forward WebSocket control frames upstream, so the
    # client's pings never get a pong and the connection drops at the
    # 20s default. The CP relay is fully bidi for binary/text frames
    # which is enough to detect dead connections via TCP semantics.
    connect_kwargs = {
        header_kwarg: headers,
        "max_size": 2 ** 20,
        "ping_interval": None,
        "ping_timeout": None,
        "close_timeout": 10,
    }
    sys.stderr.write(f"terminal-bridge: connecting to {ws_url}\n")
    try:
        async with websockets.connect(ws_url, **connect_kwargs) as ws:
            sys.stderr.write("terminal-bridge: ws connected, spawning /bin/login\n")
            pid, fd = _spawn_login()
            try:
                # FIRST_COMPLETED so a peer disconnect tears the session
                # down promptly instead of waiting for the still-blocked
                # pty read (which would hang until the child shell exits).
                done, pending = await asyncio.wait(
                    [
                        asyncio.create_task(_pty_to_ws(fd, ws)),
                        asyncio.create_task(_ws_to_pty(fd, ws)),
                    ],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                for t in done:
                    exc = t.exception()
                    if exc:
                        sys.stderr.write(f"terminal-bridge: task exited with {exc!r}\n")
                    else:
                        sys.stderr.write("terminal-bridge: task exited cleanly\n")
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
                # Reap the child so we don't leave a zombie when the
                # session ends without the child noticing the WS close.
                try:
                    os.kill(pid, 15)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
    except Exception as e:  # noqa: BLE001 — log to stderr so docker logs surfaces it
        sys.stderr.write(f"terminal-bridge: {e}\n")
        sys.exit(1)


def main() -> None:
    if len(sys.argv) != 3:
        sys.stderr.write("usage: terminal-bridge.py <ws_url> <bearer>\n")
        sys.exit(2)
    asyncio.run(run(sys.argv[1], sys.argv[2]))


if __name__ == "__main__":
    main()
