"""VictoriaLogs query tool — read structured logs by LogsQL expression.

Pairs with the reporter's Vector elasticsearch sink. Vector ships
filtered (error-level) log lines to VL; this tool lets the agent
query them back conversationally.

Example LogsQL expressions:

    level:error                          # all errors (last 1h by default)
    level:error AND container_name:vmagent
    level:error AND host:home-pi4-001
    _msg:"timeout"                       # full-text search
    container_name:reporter AND _msg:"connection refused"

Returns a list of trimmed entries (time, host, container, level, message)
inline — no dataRef pattern because logs are inherently text the user
wants to read, not arrays to chart.
"""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from ..config import settings
from .metrics import ToolContext, _parse_time


class VLClient:
    """Thin async client over the VictoriaLogs HTTP API."""

    def __init__(self, base_url: str, basic_auth: str = ""):
        self.base = base_url.rstrip("/")
        headers: dict[str, str] = {}
        if basic_auth:
            headers["Authorization"] = "Basic " + base64.b64encode(basic_auth.encode()).decode()
        self._client = httpx.AsyncClient(timeout=30.0, headers=headers)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def query(self, q: str, start: float, end: float, limit: int) -> list[dict]:
        params = {
            "query": q,
            "limit": str(limit),
            "start": datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": datetime.fromtimestamp(end, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        r = await self._client.get(f"{self.base}/select/logsql/query", params=params)
        r.raise_for_status()
        # Response is newline-delimited JSON, one entry per line.
        rows: list[dict] = []
        for raw in r.text.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                rows.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        return rows


def make_vl_client() -> VLClient | None:
    if not settings.vl_url:
        return None
    return VLClient(settings.vl_url, settings.vl_basic_auth)


# ---------- Tool implementations ----------


_MAX_MSG_LEN = 500


async def query_logs(ctx: ToolContext, args: dict) -> dict:
    if not settings.vl_url:
        return {"error": "VL_URL is not configured; the log tools are disabled"}
    if getattr(ctx, "vl", None) is None:
        return {"error": "VL client not initialized for this run"}

    q = args.get("query") or "*"
    limit = int(args.get("limit") or 50)
    if limit < 1 or limit > 500:
        return {"error": "limit must be between 1 and 500"}

    now = time.time()
    try:
        start = _parse_time(args.get("start"), now, default_offset=3600)
        end = _parse_time(args.get("end"), now, default_offset=0)
    except ValueError as e:
        return {"error": str(e)}
    if end <= start:
        return {"error": "end must be after start"}

    try:
        rows = await ctx.vl.query(q, start, end, limit)
    except httpx.HTTPStatusError as e:
        body = e.response.text[:300]
        return {"error": f"VL returned HTTP {e.response.status_code}: {body}"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"VL query failed: {type(e).__name__}: {e}"}

    entries: list[dict] = []
    for r in rows:
        msg = (r.get("_msg") or "")
        if len(msg) > _MAX_MSG_LEN:
            msg = msg[:_MAX_MSG_LEN] + "…"
        entries.append({
            "time": r.get("_time"),
            "host": r.get("host"),
            "container": r.get("container_name"),
            "level": r.get("level"),
            "message": msg,
        })

    return {
        "query": q,
        "limit": limit,
        "n": len(entries),
        "time_window": {
            "start": datetime.fromtimestamp(start, tz=timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(end, tz=timezone.utc).isoformat(),
        },
        "entries": entries,
        "note": (
            "Hit the limit — narrow the query (add container_name / host / a message "
            "substring) or shrink the time window."
            if len(entries) >= limit
            else None
        ),
    }


# ---------- Schemas + handler registry ----------


LOGS_TOOLS: list[dict] = [
    {
        "name": "query_logs",
        "description": (
            "Query structured logs from VictoriaLogs (LogsQL). Use this when the user "
            "asks about errors, exceptions, or specific log content across the fleet. "
            "The reporter ships only ERROR-level lines to VL, so `level:error` is "
            "implicit when querying recent failures — but you can also add filters on "
            "`host` (machine identity, same as Drift Deploy device name), "
            "`container_name`, `image`, or a substring of the message itself "
            "(`_msg:\"timeout\"`). Combine with AND / OR. Times accept ISO 8601, unix "
            "epoch, or relative ('1h', '30m', '7d'). Default window is the last hour. "
            "Returns up to `limit` entries (default 50, max 500) trimmed to "
            "{time, host, container, level, message}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "LogsQL expression. Examples: 'level:error', "
                        "'level:error AND container_name:vmagent', "
                        "'host:home-pi4-001 AND _msg:\"timeout\"', "
                        "'_msg:\"OOM\"'. Default: '*' (all).",
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max entries to return (1-500). Default 50.",
                },
                "start": {"type": "string", "description": "Window start. Default: 1h ago."},
                "end": {"type": "string", "description": "Window end. Default: now."},
            },
        },
    },
]


LOGS_HANDLERS = {
    "query_logs": query_logs,
}
