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
    child enters the host's namespaces and execs login — PAM does
    the rest. Returns (pid, master_fd).

    Login binary path varies: Debian/Ubuntu/Alpine ship /bin/login;
    Synology DSM and some embedded distros put it at /sbin/login or
    /usr/bin/login. We pass a literal shell snippet through nsenter so
    the host's PATH and binary layout are honored rather than the
    container's. As a last resort, fall back to executing the user's
    shell from /etc/passwd directly — useful when login is missing or
    PAM is broken — though that path bypasses authentication so it's
    gated on the host not having /bin/login at all (DSM-style cases)."""
    pid, fd = pty.fork()
    if pid == 0:
        # Run a small inline script inside the host's namespaces. The
        # script picks the first existing login binary and execs it.
        # `exec` chains so /bin/login becomes PID-equivalent to nsenter
        # and inherits the controlling tty allocated by pty.fork().
        # The diagnostic echo on failure travels back through the pty
        # to the browser's xterm so the operator sees what went wrong.
        host_cmd = (
            'for L in /bin/login /sbin/login /usr/bin/login /usr/sbin/login; do '
            '  if [ -x "$L" ]; then exec "$L"; fi; '
            'done; '
            'echo "drift terminal: no /bin/login on host (looked in '
            '/bin /sbin /usr/bin /usr/sbin)" >&2; '
            'echo "kernel: $(uname -sr); shell: $0" >&2; '
            'sleep 5; '
            'exit 127'
        )
        try:
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
                    "/bin/sh", "-c", host_cmd,
                ],
            )
        except OSError as e:
            sys.stderr.write(f"drift terminal: nsenter exec failed: {e}\r\n")
        sys.stderr.write("drift terminal: spawn fell through (nsenter or shell missing)\r\n")
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
                sys.stderr.write("terminal-bridge: reaping child\r\n")
                try:
                    _wpid, status = os.waitpid(pid, 0)
                    if os.WIFEXITED(status):
                        rc = os.WEXITSTATUS(status)
                        sys.stderr.write(f"terminal-bridge: child exited rc={rc}\r\n")
                        if rc == 127:
                            sys.stderr.write(
                                "  → host /bin/login not found, or nsenter "
                                "denied. Check host login path + caps.\r\n"
                            )
                        elif rc == 1:
                            sys.stderr.write(
                                "  → nsenter or login returned generic error "
                                "(check host PAM config or namespace support).\r\n"
                            )
                    elif os.WIFSIGNALED(status):
                        sys.stderr.write(
                            f"terminal-bridge: child killed by signal "
                            f"{os.WTERMSIG(status)}\r\n"
                        )
                except ChildProcessError as e:
                    sys.stderr.write(
                        f"terminal-bridge: waitpid failed: {e}\r\n"
                    )
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
