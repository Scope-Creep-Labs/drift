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
]


EMIT_HANDLERS = {
    "make_markdown": make_markdown,
    "make_metric": make_metric,
    "make_chart": make_chart,
    "make_table": make_table,
    "make_timeline": make_timeline,
}
