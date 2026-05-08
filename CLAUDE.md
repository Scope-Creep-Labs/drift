# CLAUDE.md

Project-specific guidance for AI coding sessions (Claude Code, etc.) working on this repo.
For the full architecture see [ARCHITECTURE.md](./ARCHITECTURE.md); for setup see [README.md](./README.md).

---

## What this repo is

**Drift** — agentic observability for time-series systems. User asks free-form questions in the UI; a Claude Opus 4.7 agent picks tools, queries a Prometheus-compatible TSDB (VictoriaMetrics), runs stats, and assembles streaming render blocks (markdown, charts, tables, metric cards, timelines) that paint progressively into the UI.

Two services:

- **`src/`** — React 18 + Vite + TypeScript + MUI v6 (dark) + Plotly + Zustand + TanStack Query.
- **`drift-agent/`** — FastAPI + `anthropic` SDK + httpx + numpy/scipy. Streams SSE.

Communication: SSE over `POST /api/investigate`. nginx proxies `/api/*` in Docker; Vite proxies in dev.

---

## Commands you'll use

**Local dev (no Docker, two terminals):**

```bash
# backend
cd drift-agent && source .venv/bin/activate
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

# frontend
npm run dev   # uses .env.local: VITE_ENGINE=agent, VITE_AGENT_DEV_URL=http://localhost:8000
```

**Docker (full stack):**

```bash
docker compose up --build                      # uses external VM_URL from .env
```

The TSDB is external — not part of this compose stack. On this host, a VM stack at `/root/setup/victoria/` is reachable via the public vmauth URL configured in `.env.example`.

**Type-check:**

```bash
npx tsc --noEmit                                                # frontend
.venv/bin/python -c "from app.main import app"                  # backend (drift-agent/)
```

**Mock mode** (UI iteration without backend or API key):

```bash
echo "VITE_ENGINE=mock" > .env.local && npm run dev
```

---

## Load-bearing conventions — DO NOT BREAK

These look like style choices but they're the architecture.

### 1. The dataRef pattern

Time-series arrays NEVER flow through the LLM context. `query_range` stores Plotly traces under a `prom://<uuid>` key in `ctx.data_cache` and pushes them to the frontend via an SSE `data` event. The LLM only sees a compact summary `{ref, n_series, series:[{n, mean, p50, p95, ...}]}`. Subsequent tools (analysis, chart emission) work by ref.

If you find yourself writing code that returns raw `x`/`y` arrays to the model: stop. Store under a ref, return the ref + a digest.

### 2. Emit blocks via tools, never raw JSON

The agent does not emit `RenderBlock` JSON in its text output. It calls `make_markdown` / `make_chart` / `make_table` / `make_metric` / `make_timeline`. Their JSON Schemas are validated by the SDK's tool layer — structure is guaranteed.

There is no JSON-from-text parsing path. Don't add one.

### 3. Prompt-cache stability

`SYSTEM_PROMPT` and the tools list (in `drift-agent/app/agent.py`) must be **byte-stable across calls**. The `cache_control: ephemeral` marker on the system prompt covers the entire `tools + system` prefix.

Things that silently invalidate the cache:
- `datetime.now()` / `time.time()` interpolated into the system prompt
- A non-deterministic tools list (e.g. iterating a `set`, dict ordering)
- Per-request UUIDs in the prefix
- Any tool definition change (the user-facing UI shows `cache hit <N> tok` per turn — if 0, audit `agent.py`)

### 4. Claude Opus 4.7 — no sampling params, no `budget_tokens`

The default model is `claude-opus-4-7` with `thinking: {type: "adaptive", display: "summarized"}` and `output_config: {effort: "high"}`. **`temperature`, `top_p`, `top_k` are removed on Opus 4.7 and return 400 if sent.** Same for `budget_tokens` — use adaptive thinking instead.

If you swap to a different model via `MODEL=...` env, check the per-model rules in the `claude-api` skill (or `shared/model-migration.md`).

### 5. SSE event ordering invariants

- `start` first, `done` last (or `error` then `done`).
- For each tool: `tool_call` precedes `tool_result` for the same `id`.
- For emit tools that touch data (`make_chart`): `data` event(s) arrive between `tool_call` and `block`.
- The frontend collapses adjacent `thinking`/`narrative` deltas into single trace entries — keep deltas as deltas, not full text.

### 6. Engine adapters are an interface, not "Mock or Agent"

`src/adapters/` is meant to be extended (Langflow, OpenAI, local model, etc.). Both shipped adapters yield the same `AgentEvent` types. Adding a new engine is a single file + one line in `getAdapter()`. The frontend doesn't change.

The `MockAdapter` exists deliberately for offline UI work — keep it working when you change the streaming protocol.

---

## Where to add things

| Goal | File(s) |
|---|---|
| New agent tool (telemetry / analysis / emit) | `drift-agent/app/tools/{metrics,analysis,emit}.py` — add handler, schema entry in `*_TOOLS`, register in `*_HANDLERS`. Optionally update `_summarize_for_event` in `agent.py`. |
| New render block type | Variant in `src/types/blocks.ts` AND `drift-agent/app/schemas.py`; React component in `src/components/blocks/`; register in `BlockRenderer.tsx`; add an emit tool in `drift-agent/app/tools/emit.py`. |
| New telemetry source (Influx, MQTT, etc.) | New file under `drift-agent/app/tools/<source>.py` mirroring `metrics.py`; add settings to `app/config.py`; register in `agent.py:all_tools()` / `all_handlers()`; mention in `SYSTEM_PROMPT`. |
| New engine | `src/adapters/<X>Adapter.ts` implementing `EngineAdapter`; wire in `getAdapter()` behind a `VITE_ENGINE` value. |
| Tweak agent behavior | `drift-agent/app/agent.py:SYSTEM_PROMPT` (prefer prompt edits over hard-coded logic — and remember §3 above). |

ARCHITECTURE.md → "Extension points" has the full version.

---

## Non-obvious gotchas

- **Persistence skips chart data.** `localStorage` holds investigations + blocks; the dataRegistry is in-memory only. Charts in past turns show "data not in cache" after a page reload — this is by design. Don't try to "fix" it by inlining trace data into blocks; that defeats the dataRef pattern.
- **`drift-agent/` editable install creates `drift_agent.egg-info/`** — gitignored.
- **Vite reads `.env.local`** (frontend) but uvicorn reads `drift-agent/.env` (backend). They're independent in local dev; only the root `.env` matters in Docker.
- **CORS is allowlist-based** in `app/main.py` via `ALLOWED_ORIGINS`. Adding a new dev origin? Update the env var.
- **The 20-iteration agent loop cap** in `agent.py` is a real ceiling. Investigations that need more than 20 LLM calls to complete will get truncated. Most finish in 4–8.

---

## Things to NOT do

- Don't add backwards-compat shims for the old `usePromptMutation` / `LangflowAdapter` / non-streaming Mock — they were deleted intentionally.
- Don't add `temperature` / `top_p` / `top_k` / `budget_tokens` to the Anthropic call.
- Don't put telemetry data in `messages` (LLM context). Use refs.
- Don't emit RenderBlocks via raw text JSON. Use the emit tools.
- Don't bake API keys, tokens, or `VM_URL` into source. They live in `.env`.
- Don't create `__pycache__/` / `.venv/` / `node_modules/` files in commits — `.gitignore` handles this; just don't bypass with `git add -f`.

---

## When a session goes off the rails

If a Claude session you're reviewing has done one of these, treat it as a flag:

- Added a JSON-parsing path to convert model text → render blocks (violates §2).
- Embedded chart `x`/`y` arrays in the LLM `messages` array (violates §1).
- Interpolated current time / a UUID into `SYSTEM_PROMPT` (violates §3).
- Set `temperature=0` or similar on Opus 4.7 (will 400).
- Switched the persisted Zustand key from `drift.investigations.v2` without a migration.
- Replaced SSE with WebSockets / polling for `/investigate` (we want SSE — see ARCHITECTURE).
