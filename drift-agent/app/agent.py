from __future__ import annotations

import json
from typing import Any, AsyncGenerator

import anthropic

from .config import settings
from .schemas import PromptRequest
from .stream import sse
from .tools.alerts import ALERT_HANDLERS, ALERT_TOOLS, make_alert_client
from .tools.analysis import ANALYSIS_HANDLERS, ANALYSIS_TOOLS
from .tools.deploy import DEPLOY_HANDLERS, DEPLOY_TOOLS
from .tools.emit import EMIT_HANDLERS, EMIT_TOOLS
from .tools.logs import LOGS_HANDLERS, LOGS_TOOLS, make_vl_client
from .tools.metrics import METRICS_HANDLERS, METRICS_TOOLS, ToolContext, make_vm_client


# Per-investigation conversation history. Keyed by investigation_id, value
# is the list[dict] of Anthropic message objects accumulated across turns.
# In-memory only — a server restart loses it. For v0 single-user usage this
# is fine; the frontend's persisted Zustand store still shows past turns
# for display, the agent just answers the next prompt fresh on a cold cache.
_session_history: dict[str, list[dict]] = {}


SYSTEM_PROMPT = """\
You are Drift — an autonomous observability investigation agent for time-series systems.

Your domain is operational telemetry: computers, IoT devices, infrastructure, edge gateways, \
industrial assets — anything that emits time-series data. You have direct access to a \
Prometheus-compatible time-series database (VictoriaMetrics) that aggregates metrics from \
multiple machines, plus quantitative-analysis tools (statistics, correlation, change-point, \
anomaly detection) and tools for emitting structured render blocks back to the user.

How you work:

1. **Discover before assuming.** Use `list_hosts`, `list_jobs`, `list_containers`, and \
`list_metrics` to confirm what's actually available before constructing PromQL. Hallucinated \
metric or label names lead to empty results and wasted iterations. Useful aggregation \
dimensions: `host` (machine identity) and `group_id` (logical grouping — client / \
cloud-vs-edge / environment / fleet). **`host` in metrics is the same string as `device` in \
Drift Deploy.** `list_hosts` returns the same identifiers as `list_devices` (e.g. \
`home-synology-001`) — no translation needed when you cross between metrics and deploy. \
For log-derived signals there are TWO complementary tools: the metric \
`container_log_lines_total{container_name, image, level}` (level ∈ error/warning/info, \
emitted by a per-host Vector collector) for COUNTS — "any container throwing errors?" \
or "did the cron-x container run in the last 24h?" — and `query_logs` (LogsQL over \
VictoriaLogs) for the actual error TEXT — "show me the error lines from the reporter \
container in the last hour". Only error-level lines are shipped to VL today; info / \
warning lines are counted-only via the metric. Prefer the metric for aggregates, \
`query_logs` for reading actual error content.

2. **Fetch data through `query_range` and `instant_query`.** Range queries return a `ref` (a \
data handle) plus a compact summary — never raw arrays. Pass refs to analysis tools and emit \
tools. This keeps your context window lean and lets the user's UI render time-series \
efficiently.

3. **Analyze quantitatively.** Use `summarize_series`, `detect_anomalies`, `correlate`, \
`compare_distributions`, and `detect_change_point` for actual math. Don't eyeball numbers \
or invent statistics — call a tool.

4. **Manage alerts when asked.** Read-only: `list_alert_rules`, `list_active_alerts`, \
`list_silences`, `list_receivers`. Rule lifecycle: `silence_alert`, `delete_silence`, \
`propose_alert_rule`, `apply_alert_rule`, `delete_alert_rule`. Receiver/route lifecycle: \
`propose_receiver`, `upsert_receiver`, `delete_receiver`, `set_route`, `delete_route`. \
When the user asks to create or change a rule OR a receiver, ALWAYS call the corresponding \
`propose_*` first, show the YAML in a `make_markdown` block, and wait for explicit \
confirmation before applying. Rules are owned in `drift-managed.yml`; hand-edited files \
are off-limits. **Receiver secrets** (webhook URLs, bearer tokens, basic-auth passwords) \
must NEVER appear in your tool input — pass FILENAMES (e.g. `auth_credentials_file: \
"ntfy-default"`), and tell the user which secret files to populate on the host. \
**Confirm before silencing** anything with a broad matcher (e.g. a bare `severity=warning`) \
— over-silencing hides real problems.

5. **Drive deployments through Drift Deploy when asked.** Discovery: \
`list_devices`, `get_device`, `list_apps`, `list_app_revisions`, `list_deployments`. \
Lifecycle: `commission_device` (returns one-time bootstrap token + a curl install \
command — present the install command verbatim in a `make_markdown` block with a code \
fence; explain the token is shown only once), `delete_device`. App management: \
`create_app`, `get_app_revision` (read the FULL file contents of an existing revision — \
use this whenever the user wants to PATCH an existing app rather than re-author it from \
scratch; fetch v_n, modify the relevant file(s) in-memory, then propose v_{n+1}), \
`propose_app_revision` (preview only — ALWAYS call this first when the user wants to \
create or change an app revision; show the would-be file list + version + sha256 in \
`make_markdown` for confirmation), `apply_app_revision` (actually packs the bundle + \
uploads), `deploy_revision` (sets desired state for ONE device — agent on the device \
picks it up within 30s), `deploy_revision_to_group` (resolves a group_id to all its \
devices and deploys to each; use this for "deploy X to all <group> devices" prompts). \
A bundle is a flat filename→contents map. The compose file must use RELATIVE paths for \
any side-files in the bundle (e.g. `./prometheus.yml`); only real host resources \
(e.g. `/var/run/docker.sock`) stay absolute. Bundles can reference per-device facts via \
`${DRIFT_DEVICE_NAME}` and `${DRIFT_GROUP_ID}` in compose env values, labels, container \
hostnames — the edge agent injects these at apply time, so one revision serves a \
heterogeneous fleet.

6. **Emit the response progressively via emit tools.** Anything you produce as plain text is \
treated as **internal reasoning** displayed to the user as a collapsed scratchpad. The \
user-visible response — narrative, charts, tables, metrics, timelines — must be assembled \
by calling `make_markdown`, `make_chart`, `make_table`, `make_metric`, and `make_timeline`. \
Emit blocks in the order you want them displayed. **This applies to every reply, including \
short conversational ones.** If you're asking a clarifying question ("what would you like \
to name it?"), acknowledging a request, or explaining you can't do something — wrap it in \
`make_markdown`. Plain text without a `make_*` tool call means the user never sees your \
reply unless they expand the Reasoning panel. Never end a turn with zero render blocks \
unless the entire response is internal and the user truly has nothing to read.

Investigation style:

- **Match response depth to question complexity.** A yes/no or single-fact question gets \
one or two blocks (a `make_markdown` plus maybe one `make_metric` or compact `make_table`). \
Save the multi-chart spread for open-ended "investigate X" / "find anomalies" prompts.
- **Lead with the answer.** Open with a one- or two-sentence `make_markdown` block that \
summarizes what you found. Then back it up with metrics, charts, and analysis below.
- **Show the data.** If you discuss a series, render it. If you claim an anomaly, mark it. \
If you compare two windows, plot both.
- **Use 2–5 metric cards** for headline numbers in deeper investigations (peak CPU, p95 \
latency, restart count, etc.).
- **Don't re-discover within a turn.** Treat `list_hosts` / `list_jobs` / `list_metrics` \
results as cached for the rest of the investigation — calling them again wastes iterations \
against the 20-call cap.
- **Don't loop on empty queries.** If a query returns no series after one corrected retry, \
say so plainly in a `make_markdown`. Don't permute label values endlessly.
- **Close with recommendations** in a final `make_markdown` when actionable.
- **Be honest about uncertainty.** If a query returned no data, say so. If a correlation is \
weak, say so. Don't invent narrative around thin evidence.

Time handling: range queries default to the last hour at 30s resolution. For longer windows \
specify `start` (ISO 8601, unix epoch, or a relative expression like `1h`/`24h`/`7d`) and a \
sensible `step` (e.g. `1m` for 6h, `5m` for 24h, `1h` for 7d).

If you cannot find relevant data after a reasonable search, emit a `make_markdown` explaining \
what you tried and what would be needed to answer the question.\
"""


def all_tools() -> list[dict]:
    deploy = DEPLOY_TOOLS if settings.deploy_enabled else []
    logs = LOGS_TOOLS if settings.vl_url else []
    return [*METRICS_TOOLS, *ALERT_TOOLS, *deploy, *logs, *ANALYSIS_TOOLS, *EMIT_TOOLS]


def all_handlers() -> dict:
    deploy = DEPLOY_HANDLERS if settings.deploy_enabled else {}
    logs = LOGS_HANDLERS if settings.vl_url else {}
    return {**METRICS_HANDLERS, **ALERT_HANDLERS, **deploy, **logs, **ANALYSIS_HANDLERS, **EMIT_HANDLERS}


def _sanitize_assistant_content(blocks: Any) -> list[dict]:
    """Re-serialize assistant blocks for inclusion in the next turn's `messages`.

    The Anthropic API rejects server-only response fields if they're echoed back
    (e.g. `parsed_output` on text blocks emitted under `output_config.effort`).
    Whitelist what we send to keep round-trips valid.
    """
    out: list[dict] = []
    for b in blocks:
        d = b.model_dump(exclude_none=True)
        t = d.get("type")
        if t == "text":
            keep = {"type": "text", "text": d.get("text", "")}
            if d.get("citations"):
                keep["citations"] = d["citations"]
            out.append(keep)
        elif t == "thinking":
            keep = {"type": "thinking", "thinking": d.get("thinking", "")}
            if d.get("signature"):
                keep["signature"] = d["signature"]
            out.append(keep)
        elif t == "redacted_thinking":
            out.append({"type": "redacted_thinking", "data": d.get("data", "")})
        elif t == "tool_use":
            out.append({
                "type": "tool_use",
                "id": d["id"],
                "name": d["name"],
                "input": d.get("input", {}),
            })
        else:
            # Unknown block type — pass through and hope the API accepts it.
            out.append(d)
    return out


def _summarize_for_event(name: str, result: Any) -> str:
    """Compact one-line preview for the UI's tool-call chip."""
    if not isinstance(result, dict):
        return type(result).__name__
    if "error" in result:
        return f"error: {result['error']}"
    if name == "list_hosts":
        return f"{result.get('n', 0)} hosts"
    if name == "list_jobs":
        return f"{result.get('n', 0)} jobs"
    if name == "list_containers":
        return f"{result.get('n', 0)} containers"
    if name == "list_metrics":
        return f"{result.get('n_total', 0)} metrics matched"
    if name == "query_range":
        ns = result.get("n_series", 0)
        first = (result.get("series") or [{}])[0]
        n = first.get("n", 0)
        return f"{ns} series · {n} pts · ref {result.get('ref','?')}"
    if name == "instant_query":
        return f"{result.get('n', 0)} rows"
    if name == "query_logs":
        return f"{result.get('n', 0)} log lines"
    if name == "list_alert_rules":
        return f"{result.get('n', 0)} rules"
    if name == "list_active_alerts":
        firing = sum(1 for a in result.get("alerts") or [] if a.get("state") == "firing")
        pending = sum(1 for a in result.get("alerts") or [] if a.get("state") == "pending")
        return f"{firing} firing · {pending} pending"
    if name == "list_silences":
        return f"{result.get('n', 0)} silences"
    if name == "list_receivers":
        return f"{result.get('n', 0)} receivers"
    if name == "silence_alert":
        return f"silenced → {result.get('silence_id', '?')[:8]}…"
    if name == "delete_silence":
        return f"deleted {result.get('deleted', '?')[:8]}…"
    if name == "propose_alert_rule":
        return f"{result.get('action', '?')} → {result.get('name', '?')}"
    if name == "apply_alert_rule":
        return f"{result.get('action', '?')} {result.get('name', '?')}"
    if name == "delete_alert_rule":
        return f"deleted {result.get('deleted', '?')}"
    if name == "propose_receiver":
        return f"{result.get('action', '?')} → {result.get('name', '?')}"
    if name == "upsert_receiver":
        return f"{result.get('action', '?')} {result.get('name', '?')}"
    if name == "delete_receiver":
        return f"deleted receiver {result.get('deleted', '?')}"
    if name == "set_route":
        return f"{result.get('action', '?')} route → {result.get('receiver', '?')}"
    if name == "delete_route":
        return f"deleted route → {result.get('deleted_for', '?')}"
    if name == "list_devices":
        return f"{result.get('n', 0)} devices"
    if name == "get_device":
        return f"{result.get('device', {}).get('name', '?')} · {len(result.get('deployments') or [])} deploys"
    if name == "commission_device":
        return f"commissioned {result.get('device', {}).get('name', '?')}"
    if name == "delete_device":
        return f"deleted {result.get('deleted', '?')}"
    if name == "list_apps":
        return f"{result.get('n', 0)} apps"
    if name == "list_app_revisions":
        return f"{result.get('app', '?')} · {result.get('n', 0)} revisions"
    if name == "get_app_revision":
        files = result.get("files") or {}
        return f"{result.get('app', '?')} v{result.get('version', '?')} · {len(files)} file(s)"
    if name == "list_deployments":
        return f"{result.get('n', 0)} deployment targets"
    if name == "create_app":
        return f"app {result.get('app', {}).get('name', '?')}"
    if name == "propose_app_revision":
        return f"{result.get('app','?')} v{result.get('next_version','?')} · {result.get('bundle_bytes', 0)} bytes"
    if name == "apply_app_revision":
        return f"{result.get('app','?')} v{result.get('version','?')} uploaded"
    if name == "deploy_revision":
        return f"{result.get('action','?')} → {result.get('device','?')}/{result.get('app','?')} v{result.get('desired_version','?')}"
    if name == "deploy_revision_to_group":
        n = len(result.get("deployed_to") or [])
        skip = len(result.get("skipped") or [])
        return f"{result.get('app','?')} v{result.get('desired_version','?')} → {n} device{'s' if n != 1 else ''} ({skip} skipped)"
    if name == "summarize_series":
        return f"{len(result.get('series') or [])} series summarized"
    if name == "detect_anomalies":
        total = sum(f.get("n_anomalies", 0) for f in result.get("findings") or [])
        return f"{total} anomalies"
    if name == "correlate":
        pairs = result.get("pairs") or []
        if pairs:
            top = pairs[0]
            return f"top |r|={abs(top['pearson_r']):.2f} ({top['a']} vs {top['b']})"
        return "no pairs"
    if name == "compare_distributions":
        return f"p50Δ={result.get('p50_delta', 0):.2f} p95Δ={result.get('p95_delta', 0):.2f}"
    if name == "detect_change_point":
        return f"{len(result.get('change_points') or [])} change points"
    if name.startswith("make_"):
        return result.get("emitted", "ok")
    return "ok"


async def run_agent(req: PromptRequest) -> AsyncGenerator[bytes, None]:
    """Run the tool-use loop and yield SSE bytes for the response stream."""
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    vm = make_vm_client()
    alerts = make_alert_client()
    vl = make_vl_client()

    events: list[bytes] = []  # outbound buffer for sync emit calls within this scope

    async def emit(event: str, data: Any) -> None:
        events.append(sse(event, data))

    ctx = ToolContext(vm=vm, emit=emit, alerts=alerts, vl=vl)
    handlers = all_handlers()
    tools = all_tools()

    user_content = req.prompt
    investigation_id: str | None = None
    if req.context:
        ctx_bits = []
        if req.context.asset_id:
            ctx_bits.append(f"asset_id={req.context.asset_id}")
        if req.context.time_range:
            ctx_bits.append(
                f"time_range={req.context.time_range.start}..{req.context.time_range.end}"
            )
        if ctx_bits:
            user_content += "\n\n[context: " + ", ".join(ctx_bits) + "]"
        investigation_id = req.context.investigation_id

    # Conversation memory: when the same investigation_id submits multiple
    # turns, prepend prior assistant/user messages so propose_*/apply_* and
    # any follow-up "ok" can chain coherently. History lives in process
    # memory only — a restart loses it; the user sees their visible turns
    # in localStorage but the agent answers their next prompt fresh.
    prior = _session_history.get(investigation_id, []) if investigation_id else []
    messages: list[dict] = [*prior, {"role": "user", "content": user_content}]

    yield sse("start", {"engine": settings.model})

    try:
        for _iteration in range(20):  # hard cap on agent loop length
            stream_kwargs = {
                "model": settings.model,
                "max_tokens": settings.max_tokens,
                "thinking": {"type": "adaptive", "display": "summarized"},
                "output_config": {"effort": settings.effort},
                "system": [
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "tools": tools,
                "messages": messages,
            }

            async with client.messages.stream(**stream_kwargs) as stream:
                async for event in stream:
                    et = getattr(event, "type", None)
                    if et == "content_block_delta":
                        d = event.delta
                        if getattr(d, "type", None) == "text_delta":
                            yield sse("narrative", {"text": d.text})
                        elif getattr(d, "type", None) == "thinking_delta":
                            yield sse("thinking", {"text": d.thinking})
                    # input_json_delta, content_block_start/stop, message_*: ignored

                final = await stream.get_final_message()

            # Drain any SSE bytes emit-tools queued during this iteration's tool execution
            # (they're queued lazily — we'll flush after running tools below).

            tool_uses = [b for b in final.content if b.type == "tool_use"]

            if not tool_uses:
                # No more tools — record the final assistant turn in
                # session history, surface metadata, and stop.
                messages.append({
                    "role": "assistant",
                    "content": _sanitize_assistant_content(final.content),
                })
                if investigation_id:
                    _session_history[investigation_id] = messages
                yield sse(
                    "metadata",
                    {
                        "engine": settings.model,
                        "stop_reason": final.stop_reason,
                        "usage": {
                            "input_tokens": final.usage.input_tokens,
                            "output_tokens": final.usage.output_tokens,
                            "cache_read_input_tokens": getattr(final.usage, "cache_read_input_tokens", 0),
                            "cache_creation_input_tokens": getattr(final.usage, "cache_creation_input_tokens", 0),
                        },
                    },
                )
                yield sse("done", {})
                return

            # Run tools and stream events.
            messages.append({"role": "assistant", "content": _sanitize_assistant_content(final.content)})

            tool_results = []
            for tu in tool_uses:
                yield sse("tool_call", {"id": tu.id, "name": tu.name, "args": tu.input})

                handler = handlers.get(tu.name)
                if handler is None:
                    result: Any = {"error": f"unknown tool: {tu.name}"}
                else:
                    try:
                        result = await handler(ctx, tu.input or {})
                    except Exception as e:  # noqa: BLE001
                        result = {"error": f"{type(e).__name__}: {e}"}

                # Flush any SSE the tool queued (block/data events from emit tools).
                while events:
                    yield events.pop(0)

                yield sse(
                    "tool_result",
                    {
                        "id": tu.id,
                        "name": tu.name,
                        "summary": _summarize_for_event(tu.name, result),
                        "is_error": isinstance(result, dict) and "error" in result,
                    },
                )

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": json.dumps(result, default=str),
                        "is_error": isinstance(result, dict) and "error" in result,
                    }
                )

            messages.append({"role": "user", "content": tool_results})

        yield sse("error", {"error": "agent loop exceeded iteration cap"})
        yield sse("done", {})
    except anthropic.APIError as e:
        yield sse("error", {"error": f"anthropic_api_error: {e}"})
        yield sse("done", {})
    except Exception as e:  # noqa: BLE001
        yield sse("error", {"error": f"{type(e).__name__}: {e}"})
        yield sse("done", {})
    finally:
        await vm.aclose()
        await alerts.aclose()
        if vl is not None:
            await vl.aclose()
