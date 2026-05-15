from __future__ import annotations

import base64
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import numpy as np

from ..config import settings


# ---------- VictoriaMetrics HTTP client ----------


class VMClient:
    def __init__(self, base_url: str, basic_auth: str = "", bearer: str = ""):
        self.base = base_url.rstrip("/")
        headers: dict[str, str] = {}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        elif basic_auth:
            token = base64.b64encode(basic_auth.encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        self._client = httpx.AsyncClient(timeout=30.0, headers=headers)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def query_range(self, promql: str, start: float, end: float, step: str) -> dict:
        r = await self._client.get(
            f"{self.base}/api/v1/query_range",
            params={"query": promql, "start": start, "end": end, "step": step},
        )
        r.raise_for_status()
        return r.json()

    async def instant_query(self, promql: str, ts: float | None = None) -> dict:
        params: dict[str, Any] = {"query": promql}
        if ts is not None:
            params["time"] = ts
        r = await self._client.get(f"{self.base}/api/v1/query", params=params)
        r.raise_for_status()
        return r.json()

    async def label_values(self, label: str, match: str | None = None) -> list[str]:
        params: dict[str, Any] = {}
        if match:
            params["match[]"] = match
        r = await self._client.get(f"{self.base}/api/v1/label/{label}/values", params=params)
        r.raise_for_status()
        return r.json().get("data", [])


# ---------- Time parsing ----------

_DURATION = re.compile(r"^(\d+)\s*([smhd])$")


def _parse_relative(s: str, now: float) -> float:
    """Parse '1h', '30m', '7d' as 'now minus that duration'."""
    m = _DURATION.match(s)
    if not m:
        raise ValueError(f"unrecognized relative time: {s}")
    n, unit = int(m.group(1)), m.group(2)
    seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit] * n
    return now - seconds


def _parse_time(s: str | None, now: float, default_offset: float = 0) -> float:
    if s is None:
        return now - default_offset
    if _DURATION.match(s):
        return _parse_relative(s, now)
    try:
        return float(s)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError as e:
        raise ValueError(f"invalid time: {s}") from e


# ---------- Tool Context ----------


class ToolContext:
    """Per-request mutable state shared with every tool call."""

    def __init__(self, vm: VMClient, emit, alerts: Any = None, vl: Any = None):
        self.vm = vm
        self.emit = emit  # async fn(event: str, data: dict)
        self.alerts = alerts  # AlertClient | None — optional
        self.vl = vl          # VLClient | None — optional
        self.data_cache: dict[str, list[dict]] = {}  # ref → Plotly trace list
        self.tags_cache: dict[str, dict] = {}  # ref → metadata for the agent

    def store(self, traces: list[dict], summary: dict) -> str:
        ref = f"prom://{uuid.uuid4().hex[:10]}"
        self.data_cache[ref] = traces
        self.tags_cache[ref] = summary
        return ref


# ---------- Helpers: Prom result → Plotly traces + summary ----------


def _prom_to_traces(result: dict, name_prefix: str = "") -> tuple[list[dict], dict]:
    """Convert a Prometheus matrix result into Plotly traces + a summary digest.

    The agent receives the digest; the traces stay in the data cache for the frontend.
    """
    traces: list[dict] = []
    series_summaries: list[dict] = []
    data = result.get("data", {})
    for series in data.get("result", []):
        metric = series.get("metric", {})
        label = (
            metric.get("__name__", "value")
            + ("{" + ",".join(f"{k}={v}" for k, v in metric.items() if k != "__name__") + "}"
               if any(k != "__name__" for k in metric) else "")
        )
        if name_prefix:
            label = f"{name_prefix} · {label}" if label else name_prefix
        values = series.get("values") or []
        if not values:
            continue
        ts = [datetime.fromtimestamp(float(p[0]), tz=timezone.utc).isoformat() for p in values]
        ys = [float(p[1]) if p[1] not in (None, "NaN") else None for p in values]
        traces.append(
            {
                "type": "scatter",
                "mode": "lines",
                "name": label,
                "x": ts,
                "y": ys,
            }
        )
        finite = np.array([v for v in ys if v is not None and np.isfinite(v)])
        if finite.size:
            series_summaries.append(
                {
                    "name": label,
                    "n": int(finite.size),
                    "min": float(finite.min()),
                    "max": float(finite.max()),
                    "mean": float(finite.mean()),
                    "p50": float(np.percentile(finite, 50)),
                    "p95": float(np.percentile(finite, 95)),
                    "first": float(finite[0]),
                    "last": float(finite[-1]),
                }
            )
        else:
            series_summaries.append({"name": label, "n": 0})
    summary = {"n_series": len(traces), "series": series_summaries}
    return traces, summary


# ---------- Tool implementations ----------


async def list_hosts(ctx: ToolContext, _input: dict) -> dict:
    # `host` is the device-identity external_label set by every reporter
    # (vmagent --external_labels.host=$DEVICE_NAME). It matches the
    # device names exposed by Drift Deploy's list_devices, so metric
    # queries and deploy queries share one identifier.
    #
    # `instance` is the scrape TARGET (cadvisor:8080, vector:9598, ...)
    # and isn't useful for "where is X running" questions. If a host
    # somehow lacks the host label (legacy data, third-party exporter),
    # we fall back to instance values to avoid an empty list.
    hosts = await ctx.vm.label_values("host")
    if not hosts:
        hosts = await ctx.vm.label_values("instance", match='up{job!=""}')
    return {"hosts": sorted(hosts), "n": len(hosts)}


async def list_jobs(ctx: ToolContext, _input: dict) -> dict:
    jobs = await ctx.vm.label_values("job")
    return {"jobs": sorted(jobs), "n": len(jobs)}


async def list_containers(ctx: ToolContext, args: dict) -> dict:
    host = args.get("host")
    match = (
        f'container_last_seen{{instance="{host}",name!=""}}'
        if host
        else 'container_last_seen{name!=""}'
    )
    names = await ctx.vm.label_values("name", match=match)
    return {"containers": sorted(names), "n": len(names), "host": host}


async def list_metrics(ctx: ToolContext, args: dict) -> dict:
    """Return metric names matching an optional substring — bounded for brevity."""
    needle = (args.get("contains") or "").lower()
    names = await ctx.vm.label_values("__name__")
    if needle:
        names = [n for n in names if needle in n.lower()]
    names.sort()
    return {"metrics": names[:50], "n_total": len(names), "truncated_to": min(50, len(names))}


async def query_range(ctx: ToolContext, args: dict) -> dict:
    promql = args["promql"]
    now = time.time()
    start = _parse_time(args.get("start"), now, default_offset=3600)
    end = _parse_time(args.get("end"), now, default_offset=0)
    step = args.get("step") or "30s"
    if end <= start:
        return {"error": "end must be after start"}
    result = await ctx.vm.query_range(promql, start, end, step)
    if result.get("status") != "success":
        return {"error": str(result.get("error") or result.get("errorType") or "query failed")}
    traces, summary = _prom_to_traces(result, name_prefix=args.get("label", ""))
    if not traces:
        return {"warning": "query returned no data", "promql": promql}
    ref = ctx.store(traces, summary)
    await ctx.emit("data", {"ref": ref, "traces": traces})
    return {
        "ref": ref,
        "promql": promql,
        "step": step,
        "time_window": {
            "start": datetime.fromtimestamp(start, tz=timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(end, tz=timezone.utc).isoformat(),
        },
        **summary,
    }


async def instant_query(ctx: ToolContext, args: dict) -> dict:
    promql = args["promql"]
    result = await ctx.vm.instant_query(promql)
    if result.get("status") != "success":
        return {"error": str(result.get("error") or "query failed")}
    rows: list[dict] = []
    for v in result.get("data", {}).get("result", []):
        metric = v.get("metric", {})
        value = v.get("value", [None, None])
        rows.append({"labels": metric, "value": float(value[1]) if value[1] is not None else None})
    return {"promql": promql, "n": len(rows), "results": rows[:25]}


# ---------- Schemas ----------

METRICS_TOOLS: list[dict] = [
    {
        "name": "list_hosts",
        "description": (
            "List all hosts (devices) reporting metrics. Returns the values of the "
            "`host` external_label that every reporter attaches. These names match "
            "the device names in Drift Deploy (list_devices), so you can pivot "
            "freely between metric queries and deployment state without re-mapping."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_jobs",
        "description": "List Prometheus 'job' label values (scrape jobs).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_containers",
        "description": (
            "List Docker container names known to cAdvisor. Optionally filter to a host. "
            "Use this before querying container_* metrics so you have valid label values."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {
                    "type": "string",
                    "description": "Optional 'instance' label value to scope by (e.g. pi:9100).",
                },
            },
        },
    },
    {
        "name": "list_metrics",
        "description": (
            "Discover metric names available in the time-series database. "
            "Use to check whether a metric exists before querying. "
            "Returns up to 50 names; pass `contains` to filter."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contains": {
                    "type": "string",
                    "description": "Case-insensitive substring filter (e.g. 'cpu', 'container').",
                },
            },
        },
    },
    {
        "name": "query_range",
        "description": (
            "Run a PromQL range query against VictoriaMetrics. Stores the resulting "
            "time-series in a per-request cache and returns a reference (ref) plus a "
            "summary of the data. Use refs as inputs to analysis tools and chart emitters — "
            "do NOT request raw arrays back. Times accept ISO 8601, unix epoch, or relative "
            "expressions like '1h' / '30m' / '7d' (interpreted as 'now minus that duration')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "promql": {"type": "string", "description": "The PromQL expression to evaluate."},
                "start": {"type": "string", "description": "Range start. Default: 1h ago."},
                "end": {"type": "string", "description": "Range end. Default: now."},
                "step": {
                    "type": "string",
                    "description": "Resolution step. Default: 30s. Use 1m+ for ranges > 1h.",
                },
                "label": {
                    "type": "string",
                    "description": "Optional friendly name prefix for the resulting series.",
                },
            },
            "required": ["promql"],
        },
    },
    {
        "name": "instant_query",
        "description": (
            "Run a PromQL instant query (single point in time, default = now). "
            "Returns up to 25 result rows with their labels and values. "
            "Use for top-K, count-by, or current-value lookups."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "promql": {"type": "string", "description": "The PromQL expression."},
            },
            "required": ["promql"],
        },
    },
]


METRICS_HANDLERS = {
    "list_hosts": list_hosts,
    "list_jobs": list_jobs,
    "list_containers": list_containers,
    "list_metrics": list_metrics,
    "query_range": query_range,
    "instant_query": instant_query,
}


def make_vm_client() -> VMClient:
    return VMClient(
        base_url=settings.vm_base,
        basic_auth=settings.vm_basic_auth,
        bearer=settings.vm_bearer_token,
    )
