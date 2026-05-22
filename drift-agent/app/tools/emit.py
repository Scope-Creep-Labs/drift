from __future__ import annotations

from typing import Any

from .metrics import ToolContext


async def make_markdown(ctx: ToolContext, args: dict) -> dict:
    block = {"type": "markdown", "content": args["content"]}
    await ctx.emit("block", block)
    return {"emitted": "markdown", "chars": len(args["content"])}


async def make_metric(ctx: ToolContext, args: dict) -> dict:
    block: dict[str, Any] = {
        "type": "metric",
        "label": args["label"],
        "value": args["value"],
    }
    if args.get("unit"):
        block["unit"] = args["unit"]
    if args.get("trend"):
        block["trend"] = args["trend"]
    await ctx.emit("block", block)
    return {"emitted": "metric", "label": args["label"]}


async def make_chart(ctx: ToolContext, args: dict) -> dict:
    refs: list[str] = args["refs"]
    unknown = [r for r in refs if r not in ctx.data_cache]
    if unknown:
        return {"error": f"unknown refs: {unknown}"}

    primary_ref = refs[0]
    if len(refs) > 1:
        # Combine traces from multiple refs into one synthetic ref the frontend can resolve.
        merged = []
        for r in refs:
            merged.extend(ctx.data_cache[r])
        merged_ref = f"prom://merged/{abs(hash(tuple(refs))):x}"
        ctx.data_cache[merged_ref] = merged
        await ctx.emit("data", {"ref": merged_ref, "traces": merged})
        primary_ref = merged_ref

    layout = args.get("layout") or {}
    block: dict[str, Any] = {
        "type": "chart",
        "renderer": "plotly",
        "spec": {"layout": layout},
        "dataRef": primary_ref,
    }
    if args.get("title"):
        block["title"] = args["title"]
    await ctx.emit("block", block)
    return {"emitted": "chart", "title": args.get("title"), "n_traces_total": sum(len(ctx.data_cache[r]) for r in refs)}


async def make_table(ctx: ToolContext, args: dict) -> dict:
    block: dict[str, Any] = {
        "type": "table",
        "columns": args["columns"],
        "rows": args["rows"],
    }
    if args.get("title"):
        block["title"] = args["title"]
    await ctx.emit("block", block)
    return {"emitted": "table", "rows": len(args["rows"])}


async def make_timeline(ctx: ToolContext, args: dict) -> dict:
    block: dict[str, Any] = {"type": "timeline", "events": args["events"]}
    if args.get("title"):
        block["title"] = args["title"]
    await ctx.emit("block", block)
    return {"emitted": "timeline", "events": len(args["events"])}


async def make_terminal_action(ctx: ToolContext, args: dict) -> dict:
    # No-op against the DB — the actual session is created when the user
    # clicks the card and the frontend POSTs /devices/{name}/terminal.
    # Keeps abandoned "open terminal" suggestions from leaving orphaned
    # `pending` rows in terminal_sessions.
    block: dict[str, Any] = {
        "type": "terminal_action",
        "device_name": args["device_name"],
    }
    if args.get("reason"):
        block["reason"] = args["reason"]
    await ctx.emit("block", block)
    return {"emitted": "terminal_action", "device_name": args["device_name"]}


async def make_live_chart(ctx: ToolContext, args: dict) -> dict:
    # No data prefetch — the frontend polls /api/query each tick. The
    # block carries only the recipe (PromQL per trace + cadence + window).
    # chart_key gives the frontend its replace-in-place identity so a
    # later emission with the same key updates the existing Plotly
    # instance (preserving zoom/hover/axes) instead of remounting.
    block: dict[str, Any] = {
        "type": "live_chart",
        "chart_key": args["chart_key"],
        "traces": args["traces"],
        "refresh_ms": int(args.get("refresh_ms") or 5000),
        "range_seconds": int(args.get("range_seconds") or 600),
        "step_seconds": int(args.get("step_seconds") or 15),
    }
    if args.get("title"):
        block["title"] = args["title"]
    await ctx.emit("block", block)
    return {
        "emitted": "live_chart",
        "chart_key": args["chart_key"],
        "n_traces": len(args["traces"]),
        "refresh_ms": block["refresh_ms"],
    }


EMIT_TOOLS: list[dict] = [
    {
        "name": "make_markdown",
        "description": (
            "Emit a markdown text block to the user's response stream. "
            "Use for narrative explanations, conclusions, recommendations. "
            "Supports GitHub-flavored markdown including headings, lists, code blocks, bold."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
        },
    },
    {
        "name": "make_metric",
        "description": "Emit a single key metric card (label + value, optional unit and trend).",
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "value": {"description": "Numeric or string value."},
                "unit": {"type": "string"},
                "trend": {"type": "string", "enum": ["up", "down", "flat"]},
            },
            "required": ["label", "value"],
        },
    },
    {
        "name": "make_chart",
        "description": (
            "Emit a Plotly time-series chart. Pass a list of data refs from query_range; "
            "all traces from those refs are combined into one chart. Use `title` to label "
            "the panel and `layout` for Plotly axis/legend overrides (xaxis title, yaxis title, etc)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "One or more data refs (from query_range).",
                    "minItems": 1,
                },
                "title": {"type": "string"},
                "layout": {
                    "type": "object",
                    "description": "Plotly layout overrides (e.g. {xaxis: {title: 'time'}, yaxis: {title: 'CPU %'}}).",
                },
            },
            "required": ["refs"],
        },
    },
    {
        "name": "make_table",
        "description": "Emit a tabular block. Use for ranked lists, summary stats, anomaly tables.",
        "input_schema": {
            "type": "object",
            "properties": {
                "columns": {"type": "array", "items": {"type": "string"}},
                "rows": {
                    "type": "array",
                    "items": {"type": "array"},
                    "description": "Each inner array is a row, with cells matching `columns` order.",
                },
                "title": {"type": "string"},
            },
            "required": ["columns", "rows"],
        },
    },
    {
        "name": "make_timeline",
        "description": "Emit a vertical event timeline. Each event has ts (ISO 8601), label, optional severity (info|warn|error).",
        "input_schema": {
            "type": "object",
            "properties": {
                "events": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ts": {"type": "string"},
                            "label": {"type": "string"},
                            "severity": {"type": "string", "enum": ["info", "warn", "error"]},
                        },
                        "required": ["ts", "label"],
                    },
                },
                "title": {"type": "string"},
            },
            "required": ["events"],
        },
    },
    {
        "name": "make_terminal_action",
        "description": (
            "Emit a clickable card offering to open a remote SSH-style terminal "
            "to a device. Use when the user asks to 'open a terminal', 'ssh into', "
            "'shell into', 'login to' a device, or when investigating an issue "
            "would benefit from interactive host access. The user clicks the card "
            "to open the terminal; do not assume it will open automatically. The "
            "user must be a deploy-role member of the device's group; the card "
            "still renders for users without access (frontend gates the click)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "device_name": {
                    "type": "string",
                    "description": "Name of the device as it appears in `list_devices`.",
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "One-line context shown under the device name on the card "
                        "(e.g. 'docker daemon not responding — needs investigation'). "
                        "Optional; helpful when the agent is recommending the terminal."
                    ),
                },
            },
            "required": ["device_name"],
        },
    },
    {
        "name": "make_live_chart",
        "description": (
            "Emit a chart that auto-refreshes by re-running its PromQL on a timer. "
            "Use when the user asks for a 'live', 'refreshing', 'real-time', or 'auto-updating' "
            "plot. **Pass the same `chart_key` across turns to update the same chart** "
            "(e.g. 'cpu-mem-jetson-001') — the frontend replaces the existing chart in place, "
            "preserving zoom/hover. A different `chart_key` creates a new chart. Each trace is "
            "an independent PromQL query (one series per query result). Do NOT use query_range "
            "before this tool — the frontend polls the queries directly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "chart_key": {
                    "type": "string",
                    "description": (
                        "Stable slug identifying this chart across edits. Reuse the slug "
                        "from the most recent make_live_chart in this conversation when "
                        "the user is modifying (refresh rate, adding/removing series, "
                        "changing window)."
                    ),
                },
                "title": {"type": "string"},
                "traces": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Legend label for this series."},
                            "promql": {"type": "string", "description": "PromQL expression — one series per matrix row in the response."},
                            "unit": {"type": "string", "description": "Optional axis unit (%, MB, req/s, ...)."},
                        },
                        "required": ["name", "promql"],
                    },
                },
                "refresh_ms": {
                    "type": "integer",
                    "minimum": 1000,
                    "default": 5000,
                    "description": "Poll interval in milliseconds. Floor 1000ms; default 5000ms.",
                },
                "range_seconds": {
                    "type": "integer",
                    "default": 600,
                    "description": "How much history each poll renders (default 600s = 10min).",
                },
                "step_seconds": {
                    "type": "integer",
                    "default": 15,
                    "description": "PromQL step in seconds (default 15s).",
                },
            },
            "required": ["chart_key", "traces"],
        },
    },
]


EMIT_HANDLERS = {
    "make_markdown": make_markdown,
    "make_metric": make_metric,
    "make_chart": make_chart,
    "make_table": make_table,
    "make_timeline": make_timeline,
    "make_live_chart": make_live_chart,
    "make_terminal_action": make_terminal_action,
}
