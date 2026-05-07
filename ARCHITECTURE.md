# Drift — Architecture

This document is the deep dive. For a quickstart, see [README.md](./README.md).

---

## Table of contents

1. [What Drift is](#what-drift-is)
2. [High-level architecture](#high-level-architecture)
3. [Request lifecycle](#request-lifecycle)
4. [Frontend](#frontend)
5. [Backend (drift-agent)](#backend-drift-agent)
6. [The streaming protocol](#the-streaming-protocol)
7. [The dataRef pattern](#the-dataref-pattern)
8. [The agent loop](#the-agent-loop)
9. [Tool catalog](#tool-catalog)
10. [Render blocks](#render-blocks)
11. [Engine adapter pattern](#engine-adapter-pattern)
12. [State model](#state-model)
13. [Prompt caching](#prompt-caching)
14. [Design decisions and trade-offs](#design-decisions-and-trade-offs)
15. [Extension points](#extension-points)
16. [File reference](#file-reference)

---

## What Drift is

Drift is a prompt-native observability investigation workspace. The user types a question in plain language; an LLM agent picks the right tools, queries a Prometheus-compatible time-series database (VictoriaMetrics), runs statistical analysis, and assembles a structured response — markdown, charts, tables, metric cards, timelines — that streams progressively into the UI as the investigation unfolds.

Two design principles drive everything else:

1. **The user types prompts, never queries.** No PromQL editor, no SQL, no notebook cells. Just a question. The agent decides what to fetch and how to analyze it.
2. **The user sees the investigation, not just the answer.** Every tool call, every fetched series, every analysis step streams to the UI as it happens. The user can watch the agent work and trust the result because they saw how it was reached.

What Drift is **not**:

- Not a metrics dashboard (Grafana). The output is per-question, not pinned panels.
- Not a notebook (Jupyter). No code cells; no programming model exposed to the user.
- Not a chat interface (ChatGPT). Responses are structured render blocks, not free-form prose.
- Not Prometheus-specific. The backend's "metrics" tools front any Prometheus-compatible TSDB; the architecture allows adding Influx / MQTT / CSV / asset-API tools alongside.

---

## High-level architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                       Browser (React + MUI)                          │
│                                                                      │
│   PromptInput ──► useInvestigate ──► AgentAdapter                    │
│                                            │                         │
│                                            │  fetch /api/investigate │
│                                            │  Server-Sent Events     │
│                                            ▼                         │
└────────────────────────────────────────────┼────────────────────────┘
                                              │
            ┌─────────────────────────────────┘
            │  (in Docker: nginx; in dev: Vite proxy)
            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                drift-agent (FastAPI · Python 3.12)                   │
│                                                                      │
│   POST /investigate ──► run_agent (SSE generator)                    │
│                              │                                       │
│                              ▼                                       │
│   Anthropic SDK · async tool-use loop · prompt caching               │
│                              │                                       │
│            ┌─────────────────┼──────────────────┐                    │
│            ▼                 ▼                  ▼                    │
│       metrics tools     analysis tools     emit tools                │
│       (httpx → VM)      (numpy + scipy)    (push SSE events)         │
│                                                                      │
└──────────────────┬──────────────────────────────────────────────────┘
                   │
                   ▼
        ┌────────────────────┐
        │   VictoriaMetrics  │  ← scrapes node-exporter + cAdvisor
        │   (or your real    │     (compose --profile demo)
        │    aggregator)     │     or your real fleet
        └────────────────────┘
```

Three deployable services in `docker-compose.yml`:

| Service          | Default | What it does                                                   |
| ---------------- | ------- | -------------------------------------------------------------- |
| `drift-agent`    | always  | FastAPI backend, agent loop, tools, SSE endpoint               |
| `drift-frontend` | always  | nginx serving the built SPA + reverse-proxying `/api/*`        |
| `victoriametrics`| `demo`  | Single-node VM; scrapes the demo `node-exporter` and `cadvisor` |
| `node-exporter`  | `demo`  | Host-level metrics from the compose host                        |
| `cadvisor`       | `demo`  | Per-container metrics from the Docker daemon                    |

---

## Request lifecycle

What happens between "user hits Enter" and "all blocks rendered":

```
1. PromptInput → useInvestigate.submit({prompt})
2. useInvestigate calls store.beginStream() — creates an empty StreamingTurn slot
3. Calls getAdapter().stream(req) → AsyncIterable<AgentEvent>
4. AgentAdapter POSTs to /api/investigate, parses SSE
5. drift-agent: run_agent generator opens an Anthropic streaming Messages call
6. For each LLM stream event:
     - text_delta  → SSE event 'narrative'
     - thinking    → SSE event 'thinking'
7. When the LLM emits tool_use blocks, run_agent waits for stream end, then:
     - For each tool_use: emit 'tool_call', execute handler, emit 'tool_result'
       (emit handlers for emit-tools also push 'block' / 'data' events)
     - Append tool_result messages to the conversation
     - Loop back to step 5 (cap: 20 iterations)
8. When the LLM finishes with no tool_use: emit 'metadata' + 'done'
9. AgentAdapter yields each event up; useInvestigate dispatches store actions:
     - thinking/narrative  → appendThinking / appendNarrative (collapses adjacent)
     - tool_call/tool_result → upsertToolCall / finishToolCall
     - data                → dataRegistry.put(ref, traces)
     - block               → addBlock
     - metadata            → setStreamMetadata
     - done                → finalizeStream  (promotes streaming → permanent Turn)
10. UI re-renders on each store update; charts resolve their dataRef from
    dataRegistry; the Scratchpad shows the trace as a collapsible panel above
    the rendered blocks.
```

---

## Frontend

Stack:

- **Build**: Vite + React 18 + TypeScript (SPA, no SSR).
- **State**: Zustand with `persist` middleware → `localStorage`.
- **Server-state**: TanStack Query for `dataRef` resolution.
- **UI**: Material UI v6 (dark theme) + custom theme tokens in `src/theme.ts`.
- **Charts**: `react-plotly.js` + `plotly.js-dist-min`, lazy-loaded.
- **Markdown**: `react-markdown` + `remark-gfm`.
- **Dev proxy**: Vite forwards `/api/*` to `VITE_AGENT_DEV_URL`.

Layered structure:

```
src/
├── types/                 — discriminated unions: RenderBlock, AgentEvent, EngineAdapter
├── adapters/              — EngineAdapter implementations (Mock, Agent)
├── data/                  — mock scenarios + dataRef registry
├── lib/                   — sseParser (more helpers as needed)
├── query/                 — TanStack Query setup + useInvestigate hook + useDataRef
├── state/                 — Zustand store (with streaming slot)
├── components/
│   ├── Shell.tsx          — 3-pane layout
│   ├── Sidebar/           — investigation history
│   ├── PromptInput.tsx    — bottom-docked textarea, ⌘/Ctrl+Enter, cancel
│   ├── Conversation.tsx   — scrolling list of permanent + streaming turns
│   ├── Turn.tsx           — one (prompt, response) pair
│   ├── Scratchpad.tsx     — collapsible thinking/tool-call trace
│   └── blocks/            — one component per RenderBlock type
└── theme.ts               — MUI dark theme
```

The frontend is intentionally engine-agnostic. Every adapter exposes the same `AsyncIterable<AgentEvent>` interface, so the UI doesn't know whether events came from a real LLM or a synthetic mock generator.

---

## Backend (drift-agent)

Stack:

- **Server**: FastAPI on `python:3.12-slim`, served by `uvicorn` with `uvloop`.
- **LLM**: `anthropic` SDK, `claude-opus-4-7` by default.
- **HTTP client**: `httpx` (async) for VictoriaMetrics.
- **Stats**: `numpy` + `scipy.stats` for analysis tools.
- **Config**: `pydantic-settings` reading `.env`.

Layered structure:

```
drift-agent/app/
├── main.py        — FastAPI app, /healthz, /investigate
├── agent.py       — System prompt, run_agent generator, tool dispatch
├── stream.py      — sse() helper for formatting Server-Sent Events
├── config.py      — Settings (pydantic-settings) + vm_base derivation
├── schemas.py     — PromptRequest, RenderBlock variants (mirror frontend types)
└── tools/
    ├── metrics.py   — VMClient, ToolContext, discovery + query tools
    ├── analysis.py  — summarize_series, detect_anomalies, correlate, ...
    └── emit.py      — make_markdown / chart / table / metric / timeline
```

The agent is **stateless across requests**. Each `/investigate` call creates a fresh `ToolContext` containing:

- A fresh `VMClient` (closed in a `finally`).
- A fresh `data_cache: dict[ref, plotly_traces]`.
- An `emit` callable that pushes SSE events back through the response stream.

There is no per-user session, no D1, no R2. Investigation history lives in the browser's `localStorage`.

---

## The streaming protocol

All events are Server-Sent Events:

```
event: <type>
data: <json>

event: ...
```

Event types and their JSON shape:

| Event           | Payload                                                                     | Frontend action                                  |
| --------------- | --------------------------------------------------------------------------- | ------------------------------------------------ |
| `start`         | `{engine: string}`                                                          | nothing rendered; metadata for diagnostics       |
| `thinking`      | `{text: string}` (delta)                                                    | append to streaming Scratchpad as a `thinking` chunk |
| `narrative`     | `{text: string}` (delta)                                                    | append to streaming Scratchpad as a `narrative` chunk |
| `tool_call`     | `{id, name, args}`                                                          | render a tool-call chip in pending state         |
| `tool_result`   | `{id, name, summary, is_error}`                                             | flip the chip to done/error with a summary       |
| `data`          | `{ref, traces}` — `traces` is an array of Plotly trace objects              | `dataRegistry.put(ref, traces)`                  |
| `block`         | A full `RenderBlock` JSON                                                   | append to the streaming turn's blocks            |
| `metadata`      | `{engine, stop_reason?, usage?}`                                            | attach to the streaming turn                     |
| `done`          | `{}`                                                                        | finalize streaming turn → permanent Turn         |
| `error`         | `{error: string}`                                                           | set error on the streaming turn (still finalizes) |

**Ordering invariants:**

- `start` is always first.
- `tool_call` for an id always precedes `tool_result` for the same id.
- For an emit tool (`make_chart`, `make_markdown`, etc.), the `block` event arrives between the `tool_call` and the `tool_result`. For emit tools that touch data (`make_chart`), one or more `data` events arrive just before the `block`.
- `done` (or `error` followed by `done`) is always last.

**Why SSE instead of WebSockets:** the data flow is one-directional (server → client), SSE is HTTP/1.1 native, debuggable with `curl -N`, and trivially proxied by nginx with `proxy_buffering off`. No need for the bidirectional ceremony of WebSockets.

---

## The dataRef pattern

Spec line 388: *"Do not embed massive telemetry arrays directly into notebook state."*

Time-series queries can return tens of thousands of points. Two things must NOT happen:

1. **Telemetry arrays should not flow through the LLM context.** A 30-minute range at 30s step is 60 points per series; a 7-day range at 1m step is 10,080. The token cost is wasteful and noisy.
2. **Telemetry arrays should not bloat investigation state in localStorage.** Investigations should serialize compactly so history scales.

The dataRef pattern solves both:

```
Backend                                       Frontend
─────────────────────────────────────         ───────────────────────────
query_range(promql, start, end, step)
  fetch from VM
  ├─ store traces under uuid-keyed ref ────►  receive 'data' event:
  │     ctx.data_cache["prom://abc123"]         dataRegistry.put(ref, traces)
  │
  └─ return to LLM:
        {ref: "prom://abc123",
         n_series: 1, n_points: 240,
         time_window: ...,
         series: [{name, n, mean, p50, p95, ...}]}

The LLM only sees the summary digest. It passes the ref to:

  detect_anomalies(ref="prom://abc123") ────►  (no UI side effect — internal)
  make_chart(refs=["prom://abc123"]) ───────►  emit 'block' event:
                                                ChartBlock with dataRef
                                              charts resolve dataRef
                                              via useDataRef → registry
```

The data thus flows **directly** from the agent to the UI in a `data` SSE event, sidestepping the LLM. The LLM stitches blocks together by passing refs around like file handles.

**Persistence caveat.** The dataRegistry is in-memory only. After a page reload, past investigations still render their structure (markdown, tables, metrics, timelines), but ChartBlocks whose dataRef is no longer in the registry display "Chart data is no longer in cache; re-run the prompt." This is an explicit trade-off — fixing it would require persisting trace bundles per turn, which we may add later. For demo flow, re-prompting is fine.

---

## The agent loop

Pseudocode of `app/agent.py:run_agent`:

```python
messages = [{"role": "user", "content": prompt}]
yield SSE("start", {"engine": MODEL})

for iteration in range(20):  # hard cap
    async with client.messages.stream(
        model=MODEL,                                  # claude-opus-4-7
        max_tokens=64000,
        thinking={"type": "adaptive", "display": "summarized"},
        output_config={"effort": "high"},
        system=[{"type": "text", "text": SYSTEM,
                 "cache_control": {"type": "ephemeral"}}],
        tools=ALL_TOOLS,
        messages=messages,
    ) as stream:
        async for event in stream:
            if text_delta: yield SSE("narrative", {"text": ...})
            if thinking_delta: yield SSE("thinking", {"text": ...})
        final = await stream.get_final_message()

    tool_uses = [b for b in final.content if b.type == "tool_use"]

    if not tool_uses:
        yield SSE("metadata", {usage, stop_reason, ...})
        yield SSE("done", {})
        return

    messages.append({"role": "assistant", "content": final.content})

    tool_results = []
    for tu in tool_uses:
        yield SSE("tool_call", {id, name, args})
        result = await TOOL_HANDLERS[tu.name](ctx, tu.input)
        # emit-tool side effects (block/data events) flushed here
        yield SSE("tool_result", {id, name, summary, is_error})
        tool_results.append({type: "tool_result", tool_use_id, content: json(result)})

    messages.append({"role": "user", "content": tool_results})
```

Key choices:

- **Claude Opus 4.7** for best-in-class agentic tool use and reliability of structured tool calls.
- **Adaptive thinking** with `display: "summarized"` so the model's reasoning streams to the user as `thinking` events.
- **Effort `high`** by default. Configurable via `EFFORT` env var (`low | medium | high | xhigh | max`).
- **Manual streaming loop** rather than the SDK's tool runner — we need fine-grained event control to forward each delta and tool call to the SSE stream.
- **20-iteration cap** prevents runaway loops. Most investigations finish in 4–8 LLM turns.
- **System prompt is stable** across calls; combined with `cache_control: ephemeral`, the tool definitions + system prompt cache hit rate is near-100% within a 5-minute window.

---

## Tool catalog

Tools fall into three buckets. The LLM sees all 16 tools simultaneously and chooses among them.

### Discovery tools (cheap, narrow context)

| Name              | Purpose                                                       |
| ----------------- | ------------------------------------------------------------- |
| `list_hosts`      | Returns `instance` label values for `up{}`.                    |
| `list_jobs`       | Returns `job` label values.                                    |
| `list_containers` | Returns `name` label values for cAdvisor's `container_last_seen`. Optionally filtered by host. |
| `list_metrics`    | Returns metric names matching an optional substring (capped at 50). |

The system prompt tells the model to always discover before assuming names. This dramatically reduces the rate of empty-result PromQL queries.

### Query tools (data ingestion)

| Name            | Purpose                                                            |
| --------------- | ------------------------------------------------------------------ |
| `query_range`   | PromQL range query → registers traces under a `prom://` ref + returns summary. Times accept ISO 8601, unix, or relative (`1h`, `24h`). |
| `instant_query` | PromQL instant query → returns up to 25 result rows directly to the LLM. Use for top-K, count-by, current-value lookups. |

Range query returns a digest like:
```json
{
  "ref": "prom://abc123",
  "promql": "rate(node_cpu_seconds_total[1m])",
  "step": "30s",
  "time_window": {"start": "2026-05-07T08:30:00+00:00", "end": "2026-05-07T09:30:00+00:00"},
  "n_series": 4,
  "series": [
    {"name": "...", "n": 120, "min": 0.012, "max": 0.78,
     "mean": 0.142, "p50": 0.11, "p95": 0.55, "first": 0.04, "last": 0.18}
  ]
}
```

### Analysis tools (numpy/scipy on cached refs)

| Name                    | Purpose                                                         |
| ----------------------- | --------------------------------------------------------------- |
| `summarize_series`      | n / mean / stddev / min / max / p50 / p90 / p95 / p99 / slope.  |
| `detect_anomalies`      | Z-score (default) or IQR. Returns up to 25 anomaly indices per series. |
| `correlate`             | Pearson r between every pair of series across two refs, with a ±10-step lag scan. |
| `compare_distributions` | Two-sample KS test + p50/p95/p99 deltas. Use for regression detection. |
| `detect_change_point`   | CUSUM-based change-point detection.                             |

All analysis tools take refs as input. They never echo raw arrays back to the LLM.

### Emit tools (push render blocks to the UI)

| Name             | Pushes                                                              |
| ---------------- | ------------------------------------------------------------------- |
| `make_markdown`  | A markdown block (GFM supported).                                    |
| `make_metric`    | A single metric card (label + value + optional unit + trend).        |
| `make_chart`     | A Plotly chart referencing one or more dataRefs. Supports custom layout.  |
| `make_table`     | A table (columns + rows + optional title).                           |
| `make_timeline`  | A vertical event timeline.                                           |

Emit tools have side effects: they push `block` events (and `data` events for charts) onto the SSE stream. They return a tiny ack to the model so the loop continues.

This split — *fetch / analyze / emit* — solves the structured-output reliability problem. The model never emits a raw `RenderBlock` JSON. Instead it calls a tool whose contract is enforced by the SDK's tool-calling layer, and the backend assembles the actual block. The model is smart about *what* to show; structure is guaranteed.

---

## Render blocks

A `RenderBlock` is a discriminated union by `type`. Five variants:

| Type      | Shape                                                                 | Component               |
| --------- | --------------------------------------------------------------------- | ----------------------- |
| `markdown`| `{type, content}`                                                     | `MarkdownBlock`         |
| `chart`   | `{type, renderer, spec, dataRef?, title?}`                            | `ChartBlock` (lazy Plotly) |
| `table`   | `{type, columns, rows, title?}`                                       | `TableBlock`            |
| `metric`  | `{type, label, value, unit?, trend?}`                                 | `MetricBlock`           |
| `timeline`| `{type, events: [{ts, label, severity?}], title?}`                    | `TimelineBlock`         |

Definitions live in two places that must stay in sync:

- `src/types/blocks.ts` — frontend TypeScript
- `drift-agent/app/schemas.py` — backend Pydantic (used for type hints; the agent emits via tools, not directly)

The agent emits blocks **only** via emit tools — never as raw JSON output. This is enforced by the system prompt and the absence of any code path that parses JSON from model text.

`BlockRenderer` groups consecutive `metric` blocks into a horizontal stack for compact display. Other block types render in arrival order.

---

## Engine adapter pattern

All engines satisfy:

```ts
interface EngineAdapter {
  stream(req: PromptRequest, signal?: AbortSignal): AsyncIterable<AgentEvent>
}
```

Two implementations ship:

| Adapter         | When                                              |
| --------------- | ------------------------------------------------- |
| `AgentAdapter`  | `VITE_ENGINE=agent` — POSTs to `/api/investigate`, parses SSE. The default in Docker. |
| `MockAdapter`   | `VITE_ENGINE=mock` — synthesizes a fake event stream from one of 5 hard-coded scenarios. Useful for offline UI work; no backend / API key needed. |

The Mock adapter exists for two reasons:
1. **UI iteration** — work on the streaming UI without burning API tokens.
2. **Architecture validation** — proves the streaming interface is genuinely engine-agnostic.

Adding a new engine (e.g., a Langflow flow, a custom Python agent, or a different LLM provider) is a single file: implement `stream(req)`, drop it in `src/adapters/`, register in `getAdapter()`. The frontend doesn't change.

---

## State model

Zustand store at `src/state/investigationStore.ts`:

```typescript
type Store = {
  investigations: Investigation[]      // persisted
  activeId: string | null              // persisted
  streaming: StreamingTurn | null      // NOT persisted (in-flight only)

  // CRUD
  createInvestigation, setActive, deleteInvestigation, renameInvestigation

  // Streaming lifecycle
  beginStream(prompt) → {investigationId, turnId}
  appendThinking(text) | appendNarrative(text)  // collapses adjacent same-kind
  upsertToolCall(id, name, args)
  finishToolCall(id, summary, isError)
  addBlock(block)
  setStreamMetadata(metadata)
  setStreamError(error)
  finalizeStream()                                // streaming → permanent Turn
  abortStream()                                   // discard
}
```

A `Turn` (permanent or streaming) holds:

```typescript
{
  id, prompt, createdAt,
  trace: TraceEntry[],     // discriminated union: thinking | narrative | tool_call
  blocks: RenderBlock[],
  metadata?, error?
}
```

Persistence: `zustand/middleware`'s `persist` writes `{investigations, activeId}` to `localStorage` under key `drift.investigations.v2`. The `streaming` slot is excluded — in-flight state should never survive a refresh. The dataRegistry (chart trace data) is not persisted either.

---

## Prompt caching

Anthropic's prompt cache is a **prefix match**. Render order is `tools → system → messages`.

The agent puts a `cache_control: {"type": "ephemeral"}` marker on the system prompt. Because tools render before system, the marker covers the entire `tools + system` prefix. As long as the tool list and system prompt are byte-stable across calls (they are — both are module-level constants), every call after the first warm-up is a cache hit on the prefix.

Visible in the UI: each turn's metadata line shows `cache hit <N> tok` when the cache fires. If that number is 0 across consecutive calls within a 5-minute window, something invalidated the prefix — check for `datetime.now()` / per-request UUIDs / non-deterministic JSON serialization in `agent.py:SYSTEM_PROMPT` or the tool list.

The `messages` array itself isn't cached because the prompt changes per call. For multi-turn investigations within one session, we could add `cache_control` to the last completed turn's content to cache the conversation prefix as well — not yet implemented; tracked as future work.

---

## Design decisions and trade-offs

**Python backend, not TypeScript / Workers.**
The analysis tools live in numpy/scipy; that ecosystem is Python-native. Cloudflare Workers can't run scipy. FastAPI is async, fast enough, and gets us a 200-line agent loop.

**Manual streaming loop, not SDK's tool runner.**
We need to forward every `text_delta`, `thinking_delta`, `tool_use`, and `tool_result` to the SSE stream. The tool runner abstracts these away. The manual loop is ~80 lines and gives us full control over event shape.

**SSE, not WebSockets.**
One-directional flow, HTTP/1.1 native, debuggable with `curl -N`. WebSockets would add bidirectional ceremony for no gain.

**dataRef pattern, not inline data.**
Time-series arrays are large; round-tripping them through the LLM context wastes tokens and bloats state. The pattern keeps the LLM working with small digests and pushes raw data straight from agent → UI.

**Emit tools, not raw JSON output.**
Asking an LLM to emit JSON in our schema is ~95% reliable. The other 5% is malformed JSON. Emit tools route through the SDK's tool-calling layer, which validates inputs against JSON Schema. The backend assembles the actual block — structure is guaranteed.

**Mock adapter as a first-class citizen.**
Lets us iterate on UI without API costs and validates that the streaming interface is engine-agnostic. Cheap insurance.

**localStorage persistence, not server-side.**
Single-user demo. Adding multi-user would require auth, a database, and a sync model — out of scope for now. `partialize` keeps the store size small (no chart data).

**Bottom-docked input, ChatGPT-style.**
Anchors the prompt while long, chart-heavy responses scroll above. Matches the user's mental model coming from ChatGPT.

**MUI dark theme.**
Operational tooling reads better dark. MUI v6 gives us a complete component library with MUI v6's improved theming. Tailwind would have been an alternative but is more work for the same outcome with this much component density.

---

## Extension points

### Add a new tool

1. Write the handler in the appropriate file (`tools/metrics.py`, `tools/analysis.py`, or `tools/emit.py`):
   ```python
   async def my_tool(ctx: ToolContext, args: dict) -> dict:
       ...
       return {"result": ...}
   ```
2. Add the JSON Schema entry to that module's `*_TOOLS` list.
3. Register the handler in the corresponding `*_HANDLERS` dict.
4. Optionally add a `_summarize_for_event` clause in `app/agent.py` so the UI's tool-call chip shows a useful one-line preview.
5. (No frontend change required — tool calls just appear with their name.)

### Add a new RenderBlock type

1. Add the variant to `src/types/blocks.ts` and `drift-agent/app/schemas.py`.
2. Write a React component under `src/components/blocks/`.
3. Register it in `src/components/blocks/BlockRenderer.tsx`.
4. Add an emit tool in `drift-agent/app/tools/emit.py`.
5. Update the system prompt's "available emit tools" section to mention it.

### Add a new telemetry source

1. Write a client + tool wrappers under `drift-agent/app/tools/<source>.py` (mirror `metrics.py`).
2. Export `<SOURCE>_TOOLS` and `<SOURCE>_HANDLERS`.
3. Register in `app/agent.py:all_tools()` and `all_handlers()`.
4. Add config to `app/config.py:Settings` (URL, auth, etc.).
5. Update the system prompt to mention the new source so the LLM uses it.

### Add a new engine

1. Implement `EngineAdapter` in `src/adapters/<Engine>Adapter.ts`.
2. Wire into `src/adapters/index.ts:getAdapter()` behind a `VITE_ENGINE=...` value.
3. Adapter should yield the standard `AgentEvent` types so the UI just works.

### Swap LLM providers

The agent loop calls Anthropic. Swapping to OpenAI / Bedrock / etc. is a refactor of `app/agent.py:run_agent`. The SSE protocol stays the same. Provider differences (streaming event names, tool-use shapes) all live inside `run_agent`.

---

## File reference

Full repository tree with one-line descriptions:

```
drift/
├── README.md                      Quickstart + dev workflow.
├── ARCHITECTURE.md                This file.
├── docker-compose.yml             3 always-up services + 3 demo-profile services.
├── Dockerfile                     Frontend: alpine node builder + nginx alpine runtime.
├── nginx.conf                     SPA fallback + /api proxy with SSE buffering disabled.
├── .dockerignore
├── .env.example                   Root-level env template (compose substitutes).
├── .gitignore
├── package.json                   Frontend dependencies (React 18, MUI 6, Plotly, Zustand, TanStack Query).
├── package-lock.json
├── tsconfig.json
├── vite.config.ts                 Dev proxy /api → VITE_AGENT_DEV_URL.
├── index.html
├── src/                           Frontend source.
│   ├── main.tsx                   React root + ThemeProvider + QueryClientProvider.
│   ├── App.tsx                    Renders <Shell />.
│   ├── theme.ts                   MUI dark theme tokens.
│   ├── vite-env.d.ts
│   ├── adapters/
│   │   ├── index.ts               getAdapter() — env-driven switch.
│   │   ├── AgentAdapter.ts        SSE-consuming adapter.
│   │   └── MockAdapter.ts         Synthetic event stream from scenario modules.
│   ├── components/
│   │   ├── Shell.tsx              3-pane layout.
│   │   ├── Conversation.tsx       Renders permanent + streaming turns + empty state.
│   │   ├── Turn.tsx               One (prompt, response) pair, accepts streaming variant.
│   │   ├── PromptInput.tsx        Bottom-docked textarea with ⌘/Ctrl+Enter and cancel.
│   │   ├── Scratchpad.tsx         Collapsible thinking/tool-call trace.
│   │   ├── Sidebar/
│   │   │   └── InvestigationList.tsx
│   │   └── blocks/
│   │       ├── BlockRenderer.tsx  Discriminated-union switch + metric grouping.
│   │       ├── MarkdownBlock.tsx
│   │       ├── ChartBlock.tsx     Lazy-loads renderer-specific impl.
│   │       ├── ChartBlock.plotly.tsx   react-plotly.js + dist-min.
│   │       ├── ChartBlock.echarts.tsx  Stub ("not yet wired").
│   │       ├── TableBlock.tsx
│   │       ├── MetricBlock.tsx
│   │       └── TimelineBlock.tsx
│   ├── data/
│   │   ├── registry.ts            dataRef store + resolve().
│   │   ├── synth.ts               Pure-TS RNG, sin/noise/walk/anomaly helpers.
│   │   └── scenarios/             5 mock investigation scenarios.
│   ├── lib/
│   │   └── sseParser.ts           Pure SSE frame parser used by AgentAdapter.
│   ├── query/
│   │   ├── client.ts              QueryClient instance.
│   │   ├── useDataRef.ts          Resolve dataRef → Plotly traces (cached).
│   │   └── useInvestigate.ts      Submit prompt → consume stream → drive store.
│   ├── state/
│   │   └── investigationStore.ts  Zustand + persist + streaming slot.
│   └── types/
│       ├── adapter.ts             EngineAdapter interface.
│       ├── agentEvents.ts         AgentEvent union + TraceEntry union.
│       ├── blocks.ts              RenderBlock union.
│       ├── plotly-dist.d.ts       Re-export plotly.js types for the dist-min build.
│       └── prompt.ts              PromptRequest / PromptResponse.
├── drift-agent/                   Python backend (no separate README — see this file).
│   ├── pyproject.toml
│   ├── Dockerfile                 python:3.12-slim multi-stage; non-root.
│   ├── .dockerignore
│   ├── .env.example
│   └── app/
│       ├── __init__.py
│       ├── main.py                FastAPI app, /healthz, /investigate.
│       ├── agent.py               System prompt + run_agent generator + tool dispatch.
│       ├── stream.py              sse() helper.
│       ├── config.py              Settings (pydantic-settings).
│       ├── schemas.py             PromptRequest + RenderBlock pydantic mirrors.
│       └── tools/
│           ├── __init__.py
│           ├── metrics.py         VMClient, ToolContext, discovery + query tools.
│           ├── analysis.py        numpy/scipy stats tools.
│           └── emit.py            make_* tools that push SSE block/data events.
├── compose/
│   └── scrape.yaml                Demo VictoriaMetrics scrape config.
└── spec/                          Original product specs (reference only — code is authoritative).
    ├── prompt_driven_observability_notebook_simplified_spec.md
    └── drift_agentic_observability_spec.md
```
