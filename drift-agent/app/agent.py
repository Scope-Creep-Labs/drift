from __future__ import annotations

import json
import os
from typing import Any, AsyncGenerator

import litellm

from .config import settings
from .schemas import PromptRequest
from .stream import sse


# Push the provider API keys into the env so LiteLLM picks them up via
# its standard env-var lookup. Only the key for the configured model's
# provider needs to be present; others stay empty. Done at module load
# so a `litellm.acompletion` call anywhere just works.
if settings.anthropic_api_key:
    os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
if settings.openai_api_key:
    os.environ["OPENAI_API_KEY"] = settings.openai_api_key
if settings.gemini_api_key:
    os.environ["GEMINI_API_KEY"] = settings.gemini_api_key

# Drop verbose default logging; we surface failures via SSE error events.
litellm.suppress_debug_info = True
from .tools.alerts import ALERT_HANDLERS, ALERT_TOOLS, make_alert_client
from .tools.analysis import ANALYSIS_HANDLERS, ANALYSIS_TOOLS
from .tools.deploy import DEPLOY_HANDLERS, DEPLOY_TOOLS
from .tools.users import USER_HANDLERS, USER_TOOLS
from .tools.emit import EMIT_HANDLERS, EMIT_TOOLS
from .tools.logs import LOGS_HANDLERS, LOGS_TOOLS, make_vl_client
from .tools.metrics import METRICS_HANDLERS, METRICS_TOOLS, ToolContext, make_vm_client


# Per-investigation conversation history. Keyed by investigation_id, value
# is the list[dict] of LiteLLM/OpenAI-shape message objects accumulated
# across turns (role ∈ {system, user, assistant, tool}). In-memory only
# — a server restart loses it. For v0 single-user usage this is fine;
# the frontend's persisted Zustand store still shows past turns for
# display, the agent just answers the next prompt fresh on a cold cache.
_session_history: dict[str, list[dict]] = {}

# Token-saving knobs for the session-history pipeline:
# - Tool results over this many bytes get truncated when persisted. The
#   full output was consumed by the assistant on the turn that produced
#   it; subsequent turns just need a hint. 4 KB ≈ 1k tokens.
_MAX_TOOL_RESULT_BYTES = 4096
# - Keep at most this many user-initiated turns per investigation. A
#   "turn" = one user prompt + all the agent activity that followed it
#   (assistant + tool_result messages) until the next user prompt.
_MAX_TURNS = 20


def _compact_history_for_save(messages: list[dict]) -> list[dict]:
    """Cap large tool-result content before persisting to session history.

    OpenAI-shape tool results live in `{role: "tool", tool_call_id, content}`
    messages; the content is a JSON-stringified payload. We truncate the
    string when it exceeds _MAX_TOOL_RESULT_BYTES — the assistant already
    consumed the full output on the turn it was produced; later turns
    only need to remember the call happened.
    """
    out: list[dict] = []
    for msg in messages:
        if msg.get("role") == "tool":
            body = msg.get("content")
            if isinstance(body, str) and len(body) > _MAX_TOOL_RESULT_BYTES:
                truncated = (
                    body[:_MAX_TOOL_RESULT_BYTES]
                    + f"\n… [tool result truncated for history: "
                      f"{len(body)} bytes total. Re-call the tool if "
                      f"you need the full output.]"
                )
                out.append({**msg, "content": truncated})
                continue
        out.append(msg)
    return out


def _trim_to_recent_turns(messages: list[dict], max_turns: int = _MAX_TURNS) -> list[dict]:
    """Drop older user-initiated turns from the front, keeping the last N.

    Cut at user-prompt boundaries (role=user) so every kept segment
    starts with a user prompt and ends with a terminal assistant
    message — preserving the assistant→tool_calls→tool→… pairing
    invariant the OpenAI/LiteLLM API requires. The optional initial
    system message is preserved by special-casing index 0.
    """
    prompt_indices = [
        i for i, m in enumerate(messages)
        if m.get("role") == "user"
    ]
    if len(prompt_indices) <= max_turns:
        return messages
    cut = prompt_indices[-max_turns]
    # If a system message leads the history, keep it in front.
    if messages and messages[0].get("role") == "system":
        return [messages[0], *messages[cut:]]
    return messages[cut:]


def _to_openai_tools(tools: list[dict]) -> list[dict]:
    """Convert Drift's Anthropic-flavored tool schemas to OpenAI's
    function-calling shape that LiteLLM expects."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object"}),
            },
        }
        for t in tools
    ]


def _normalize_usage(u: Any) -> dict[str, int]:
    """Map provider-specific usage to Drift's 4-kind shape.

    The metric labels in `drift_agent_tokens_total{kind}` are stable
    across providers. Mapping:
      - input: uncached, billed-per-1k prompt tokens.
      - output: completion / assistant tokens.
      - cache_read: prompt tokens served from a cache hit.
      - cache_creation: prompt tokens written to cache (Anthropic-only).

    OpenAI's `prompt_tokens` INCLUDES `cached_tokens` (subset). Anthropic
    via LiteLLM reports `cache_read_input_tokens` separately. We detect
    which shape we got and subtract appropriately so "input" is always
    fresh-only.
    """
    if u is None:
        return {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    prompt = getattr(u, "prompt_tokens", 0) or 0
    completion = getattr(u, "completion_tokens", 0) or 0
    # Anthropic shape (via LiteLLM):
    cache_read_anthropic = getattr(u, "cache_read_input_tokens", 0) or 0
    cache_creation = getattr(u, "cache_creation_input_tokens", 0) or 0
    # OpenAI shape:
    cache_read_openai = 0
    details = getattr(u, "prompt_tokens_details", None)
    if details is not None:
        # details can be a dict or pydantic-like object
        if isinstance(details, dict):
            cache_read_openai = details.get("cached_tokens", 0) or 0
        else:
            cache_read_openai = getattr(details, "cached_tokens", 0) or 0
    cache_read = cache_read_anthropic + cache_read_openai
    if cache_read_anthropic:
        # Anthropic via LiteLLM: prompt_tokens excludes cached; use as-is.
        input_tokens = prompt
    else:
        # OpenAI/Gemini: prompt_tokens INCLUDES cached; subtract for the
        # "input" (fresh) component.
        input_tokens = max(0, prompt - cache_read_openai)
    return {
        "input_tokens": input_tokens,
        "output_tokens": completion,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
    }


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
`query_logs` for reading actual error content. \
Drift itself emits self-observability metrics that you can query like any \
other series. Use them when the user asks about token usage, costs, conversation \
counts, or per-user activity: \
`drift_agent_tokens_total{user, model, kind}` — kind ∈ {input, output, cache_read, \
cache_creation} — and `drift_agent_turns_total{user, model}`. Both are CP-side \
counters scraped on `host=dev-hetzner, job=drift-deploy-cp`. The `model` label is \
the literal model id Drift is configured with (claude-opus-4-7, gpt-4o, \
gemini-2.5-pro, etc.); use it to slice usage by provider. When the user asks for \
$-denominated answers, ask them which model's pricing to apply (or look up current \
pricing for `model` if they say "the running model") and multiply tokens by \
`price_per_million / 1e6`. For "since when?" use `increase(...[Xh|d|w])`; for \
"right now" use the raw counter value.

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
`fork_app` (copy an existing app's revision as a NEW app's first revision in one atomic \
call — use for "make a parallel app like X" prompts, no propose dance needed since the \
bytes are deterministic), `propose_app_revision` (preview only — call this first when \
files were AUTHORED from user input or EDITED in any way, so the user can spot a bad \
LLM edit before commit; SKIP for verbatim copies where bytes come from another tool \
unchanged, like `get_app_revision` → `apply_app_revision` with no modification or \
`fork_app` which is already atomic), `apply_app_revision` (actually packs the bundle + \
uploads), `deploy_revision` (sets desired state for ONE device — agent on the device \
picks it up within 30s), `deploy_revision_to_group` (resolves a group_id to all its \
devices and deploys to each; use this for "deploy X to all <group> devices" prompts), \
`delete_deployment` / `delete_deployment_from_group` (mark a deployment for removal — \
the edge agent runs `docker compose down` on the next check-in, then the target row is \
deleted server-side once the agent confirms the stop). **ALWAYS confirm with the user \
before calling either `delete_*` tool** — list which devices will be affected in a \
`make_markdown`, then wait for explicit "yes" / "do it" before calling. Running services \
get stopped; that's hard to undo if the user mistypes the app name. \
If `deploy_revision` or `deploy_revision_to_group` returns a `warning` with \
`conflicts` (container_name collisions on the target), the only viable paths are \
REPLACE or CANCEL — there is no force. Present the conflict list to the user in a \
`make_markdown` block, name the conflicting app(s), and offer two choices: \
(1) replace — execute the `replace_plan` steps in order (delete the conflicting \
app(s), then re-issue the deploy); (2) cancel — drop the request. Wait for the \
user's pick before running any tool from the replace_plan. \
A bundle is a flat filename→contents map. The compose file must use RELATIVE paths for \
any side-files in the bundle (e.g. `./prometheus.yml`); only real host resources \
(e.g. `/var/run/docker.sock`) stay absolute. Bundles can reference per-device facts via \
`${DRIFT_DEVICE_NAME}` and `${DRIFT_GROUP_ID}` in compose env values, labels, container \
hostnames — the edge agent injects these at apply time, so one revision serves a \
heterogeneous fleet.

6. **Emit the response progressively via emit tools.** Anything you produce as plain text is \
treated as **internal reasoning** displayed to the user as a collapsed scratchpad. The \
user-visible response — narrative, charts, tables, metrics, timelines — must be assembled \
by calling `make_markdown`, `make_chart`, `make_table`, `make_metric`, `make_timeline`, or \
`make_live_chart`. Emit blocks in the order you want them displayed. **This applies to \
every reply, including short conversational ones.** If you're asking a clarifying question \
("what would you like to name it?"), acknowledging a request, or explaining you can't do \
something — wrap it in `make_markdown`. Plain text without a `make_*` tool call means the \
user never sees your reply unless they expand the Reasoning panel. Never end a turn with \
zero render blocks unless the entire response is internal and the user truly has nothing \
to read.

**Terminal access** (`make_terminal_action`): when the user asks to open a terminal, \
SSH in, shell into, or login to a device, emit a `make_terminal_action(device_name=…)` \
card. They click the card to open the in-browser terminal modal — do not promise the \
shell is already open. If suggesting it proactively (e.g. you've diagnosed an issue \
that needs interactive host investigation), add a one-line `reason` so the card \
explains itself.

**Live charts** (`make_live_chart`): use when the user asks for a refreshing / live /\
real-time plot. Skip `query_range` — the frontend polls the PromQL on its own timer. \
**Pick a stable `chart_key` slug** (e.g. `cpu-mem-jetson-001`) the first time, and on \
follow-up turns where the user is modifying that chart ("change refresh to 1s", "add \
jetson-002", "switch the window to 1h"), **emit `make_live_chart` again with the SAME \
`chart_key`** plus the updated traces / refresh_ms / range_seconds. The frontend replaces \
the existing chart in place, preserving Plotly zoom and hover. A different `chart_key` \
creates a new chart side-by-side — use that only when the user explicitly asks for a \
separate plot.

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
    # User management tools come on the same flag as deploy — they share
    # the Postgres + auth subsystem and don't make sense in pure-VM mode.
    deploy = DEPLOY_TOOLS if settings.deploy_enabled else []
    users = USER_TOOLS if settings.deploy_enabled else []
    logs = LOGS_TOOLS if settings.vl_url else []
    return [*METRICS_TOOLS, *ALERT_TOOLS, *deploy, *users, *logs, *ANALYSIS_TOOLS, *EMIT_TOOLS]


def all_handlers() -> dict:
    deploy = DEPLOY_HANDLERS if settings.deploy_enabled else {}
    users = USER_HANDLERS if settings.deploy_enabled else {}
    logs = LOGS_HANDLERS if settings.vl_url else {}
    return {
        **METRICS_HANDLERS,
        **ALERT_HANDLERS,
        **deploy,
        **users,
        **logs,
        **ANALYSIS_HANDLERS,
        **EMIT_HANDLERS,
    }


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
    if name == "fork_app":
        new = " (new app)" if result.get("target_app_created") else ""
        return f"{result.get('source_app','?')} v{result.get('source_version','?')} → {result.get('target_app','?')} v{result.get('version','?')}{new}"
    if name == "deploy_revision":
        return f"{result.get('action','?')} → {result.get('device','?')}/{result.get('app','?')} v{result.get('desired_version','?')}"
    if name == "deploy_revision_to_group":
        n = len(result.get("deployed_to") or [])
        skip = len(result.get("skipped") or [])
        return f"{result.get('app','?')} v{result.get('desired_version','?')} → {n} device{'s' if n != 1 else ''} ({skip} skipped)"
    if name == "delete_deployment":
        return f"removing {result.get('app','?')} from {result.get('device','?')}"
    if name == "delete_deployment_from_group":
        n = len(result.get("marked_for_removal") or [])
        return f"removing {result.get('app','?')} from {n} device{'s' if n != 1 else ''}"
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


async def run_agent(req: PromptRequest, user: Any = None) -> AsyncGenerator[bytes, None]:
    """Run the tool-use loop and yield SSE bytes for the response stream.

    `user` is the UserContext of the authenticated operator (None in
    test contexts). Tools that mutate deploy state consult user.role
    and user.groups; observability tools are role-agnostic.
    """
    vm = make_vm_client()
    alerts = make_alert_client()
    vl = make_vl_client()

    events: list[bytes] = []  # outbound buffer for sync emit calls within this scope

    async def emit(event: str, data: Any) -> None:
        events.append(sse(event, data))

    ctx = ToolContext(vm=vm, emit=emit, alerts=alerts, vl=vl, user=user)
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

    # Identity envelope: tell the LLM who's driving this turn and what they
    # can do, so it can politely refuse mutation requests for observe-only
    # users instead of trying a tool and getting a permission error. Goes
    # in the user message (not the system prompt) to preserve prompt cache
    # stability — system prompt + tools list must be byte-stable across
    # turns for the cache hit to register.
    if user is not None:
        role = user.role
        groups = sorted(user.groups) if user.groups else []
        if user.is_admin:
            cap = "admin (full access to deploy, observe, manage users, and registry credentials)"
            grp = "all groups"
        elif user.is_deploy:
            cap = "deploy (can deploy/update/delete apps and manage alerts)"
            grp = f"{', '.join(groups) or 'none'}"
        else:
            cap = "observe (read-only on deploys; can manage alert rules)"
            grp = f"{', '.join(groups) or 'none'}"
        user_content = (
            f"[operator: {user.username} · role={role} · access={grp} · {cap}]\n\n"
            + user_content
        )

    # Conversation memory: when the same investigation_id submits multiple
    # turns, prepend prior assistant/user/tool messages so propose_*/apply_*
    # and any follow-up "ok" can chain coherently. History is OpenAI-shape
    # (LiteLLM's canonical format). In-memory only — a restart loses it;
    # the user sees their visible turns in localStorage but the agent
    # answers their next prompt fresh.
    prior = _session_history.get(investigation_id, []) if investigation_id else []
    # System message goes first; if `prior` already begins with one we
    # don't duplicate it (we replace, since SYSTEM_PROMPT may have been
    # edited between turns and we want the latest).
    if prior and prior[0].get("role") == "system":
        prior = prior[1:]
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *prior,
        {"role": "user", "content": user_content},
    ]
    openai_tools = _to_openai_tools(tools)

    # Accumulate usage across every API call in this turn's tool loop.
    # `final.usage` only carries the LAST call's tokens — without this
    # accumulator, prior rounds (system prompt read, tool-result reads)
    # would silently drop. Emitted as the turn's `metadata.usage` and
    # exported to Prometheus when the loop concludes.
    turn_usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }

    yield sse("start", {"engine": settings.model})

    try:
        for _iteration in range(20):  # hard cap on agent loop length
            # Per-iteration accumulators for the streaming assembly.
            text_buf = ""
            tool_calls_by_idx: dict[int, dict] = {}
            finish_reason: str | None = None
            iter_usage: Any = None

            response = await litellm.acompletion(
                model=settings.model,
                messages=messages,
                tools=openai_tools,
                stream=True,
                stream_options={"include_usage": True},
                max_tokens=settings.max_tokens,
            )

            async for chunk in response:
                choices = getattr(chunk, "choices", None) or []
                if choices:
                    delta = choices[0].delta
                    if getattr(delta, "content", None):
                        text_buf += delta.content
                        yield sse("narrative", {"text": delta.content})
                    # Some providers surface internal reasoning as
                    # `reasoning_content` on the delta. LiteLLM normalizes
                    # the attribute name across Claude / o-series / Gemini.
                    rc = getattr(delta, "reasoning_content", None)
                    if rc:
                        yield sse("thinking", {"text": rc})
                    tcs = getattr(delta, "tool_calls", None) or []
                    for tc in tcs:
                        # Streaming tool_calls arrive in pieces — assemble
                        # by `index`. id + function.name appear on the
                        # first delta for a slot; function.arguments is
                        # a JSON-string stream that must be concatenated.
                        idx = getattr(tc, "index", 0) or 0
                        slot = tool_calls_by_idx.setdefault(idx, {"id": None, "name": None, "args": ""})
                        if getattr(tc, "id", None):
                            slot["id"] = tc.id
                        fn = getattr(tc, "function", None)
                        if fn is not None:
                            if getattr(fn, "name", None):
                                slot["name"] = fn.name
                            if getattr(fn, "arguments", None):
                                slot["args"] += fn.arguments
                    if choices[0].finish_reason:
                        finish_reason = choices[0].finish_reason
                # Usage rides on the final chunk when include_usage=True.
                if getattr(chunk, "usage", None):
                    iter_usage = chunk.usage

            # Roll this iteration's usage into the per-turn accumulator.
            iter_norm = _normalize_usage(iter_usage)
            for k, v in iter_norm.items():
                turn_usage[k] += v

            # Materialize the assistant message we just received so it
            # can ride along in the next iteration's `messages`.
            tool_calls: list[dict] = []
            for idx in sorted(tool_calls_by_idx.keys()):
                slot = tool_calls_by_idx[idx]
                if not slot["id"] or not slot["name"]:
                    continue  # malformed — skip
                tool_calls.append({
                    "id": slot["id"],
                    "type": "function",
                    "function": {
                        "name": slot["name"],
                        "arguments": slot["args"] or "{}",
                    },
                })

            if not tool_calls:
                # No more tools — record the final assistant turn,
                # surface metadata, and stop.
                messages.append({
                    "role": "assistant",
                    "content": text_buf,
                })
                if investigation_id:
                    compacted = _compact_history_for_save(messages)
                    _session_history[investigation_id] = _trim_to_recent_turns(compacted)

                yield sse(
                    "metadata",
                    {
                        "engine": settings.model,
                        "stop_reason": finish_reason or "stop",
                        "usage": dict(turn_usage),
                    },
                )

                # Export to Prometheus so reporter-cp picks it up. The
                # `model` label is the literal settings.model string —
                # claude-opus-4-7, gpt-4o, gemini-2.5-pro, etc. — so
                # operators can slice usage by provider.
                from .deploy.observability import agent_tokens_total, agent_turns_total
                username = getattr(user, "username", None) or "anonymous"
                for kind, attr in (
                    ("input", "input_tokens"),
                    ("output", "output_tokens"),
                    ("cache_read", "cache_read_input_tokens"),
                    ("cache_creation", "cache_creation_input_tokens"),
                ):
                    n = turn_usage[attr]
                    if n > 0:
                        agent_tokens_total.labels(
                            user=username, model=settings.model, kind=kind
                        ).inc(n)
                agent_turns_total.labels(user=username, model=settings.model).inc()

                yield sse("done", {})
                return

            # Run tools, append assistant + tool messages, stream events.
            messages.append({
                "role": "assistant",
                "content": text_buf or None,
                "tool_calls": tool_calls,
            })

            for slot in (tool_calls_by_idx[i] for i in sorted(tool_calls_by_idx)):
                if not slot["id"] or not slot["name"]:
                    continue
                try:
                    args = json.loads(slot["args"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                yield sse("tool_call", {"id": slot["id"], "name": slot["name"], "args": args})

                handler = handlers.get(slot["name"])
                if handler is None:
                    result: Any = {"error": f"unknown tool: {slot['name']}"}
                else:
                    try:
                        result = await handler(ctx, args)
                    except Exception as e:  # noqa: BLE001
                        result = {"error": f"{type(e).__name__}: {e}"}

                # Flush any SSE the tool queued (block/data events).
                while events:
                    yield events.pop(0)

                yield sse(
                    "tool_result",
                    {
                        "id": slot["id"],
                        "name": slot["name"],
                        "summary": _summarize_for_event(slot["name"], result),
                        "is_error": isinstance(result, dict) and "error" in result,
                    },
                )

                messages.append({
                    "role": "tool",
                    "tool_call_id": slot["id"],
                    "content": json.dumps(result, default=str),
                })

        yield sse("error", {"error": "agent loop exceeded iteration cap"})
        yield sse("done", {})
    except Exception as e:  # noqa: BLE001 — LiteLLM raises many provider-specific types
        yield sse("error", {"error": f"{type(e).__name__}: {e}"})
        yield sse("done", {})
    finally:
        await vm.aclose()
        await alerts.aclose()
        if vl is not None:
            await vl.aclose()
