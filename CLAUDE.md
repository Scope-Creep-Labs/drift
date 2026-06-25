# CLAUDE.md

Project-specific guidance for AI coding sessions (Claude Code, etc.) working on this repo.
For the full architecture see [ARCHITECTURE.md](./ARCHITECTURE.md); for setup see [README.md](./README.md).

---

## What this repo is

**Drift** — agentic observability + fleet management for time-series systems. User asks free-form questions in the UI; a Claude Opus 4.7 agent picks tools, queries a Prometheus-compatible TSDB (VictoriaMetrics), runs stats, and assembles streaming render blocks (markdown, charts, tables, metric cards, timelines, live-charts, terminal-actions) that paint progressively into the UI.

Three services:

- **`src/`** — React 18 + Vite + TypeScript + MUI v6 (dark) + Plotly + Zustand + xterm.js.
- **`drift-agent/`** — FastAPI + `anthropic` SDK + httpx + numpy/scipy + SQLAlchemy. Streams SSE. Owns the agent loop, user auth, deploy state, terminal relay.
- **`drift-postgres`** — durable state: users + sessions + groups, devices + apps + revisions + deployments, registry credentials, terminal session audit. Alembic migrations under `drift-agent/alembic/`.

There's also an **edge agent** (`edge-agent/`) — a bash script + python bridge that runs as a container on each managed device. Self-updates via a SHA-comparing bootstrap. Polls the CP every 30s.

Communication: SSE over `POST /api/investigate`; WS for the web terminal (`/api/deploy/.../terminal/ws/...`). nginx proxies `/api/*` in Docker; Vite proxies in dev.

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

**Full stack (production install path):**

Use `deploy/install.sh` — it generates `/var/lib/drift-cp/.env` with random secrets, renders config templates into `/var/lib/drift-cp/config/`, and brings up the compose stack from `deploy/docker-compose.yml`. The repo-root `docker-compose.yml` + matching `.env` were removed (they duplicated the deploy compose, drifted out of sync, and bit prod twice). For prod env changes, edit `/var/lib/drift-cp/.env` directly; apply via the Software Updates modal.

The TSDB (VictoriaMetrics) is external — not part of this compose stack. On this host, a VM stack at `/root/setup/victoria/` is reachable via the public vmauth URL.

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
| New deploy admin route | `drift-agent/app/deploy/routes_admin.py`. Auth via `Depends(require_role("deploy"))` or `Depends(get_current_user)`. Call `_check_group_access(user, group_id)` on any mutation that targets a device. Schema in `deploy/schemas.py`; model in `deploy/models.py`; migration in `alembic/versions/`. |
| New edge-agent behavior | `edge-agent/drift-deploy-agent.sh`. Bump `AGENT_VERSION`. Devices self-update on next check-in (≤ POLL_INTERVAL) via SHA comparison. Image-level changes (Dockerfile, terminal-bridge.py, host CA) need `install.sh` rerun on each device — those don't auto-update. |
| Bridge new capability through to apps | Compose `.env` interpolation: install.sh writes the value to `/etc/drift-deploy/env`, the agent script `export`s it into `docker compose` subshells, bundle authors reference it as `${DRIFT_…}`. Already wired: `DRIFT_DEVICE_NAME`, `DRIFT_GROUP_ID`, `DRIFT_DOCKER_DATA_DIR`, `DRIFT_HOST_CA_BUNDLE`. |
| Web-terminal change | `drift-agent/app/deploy/terminal.py` (relay), `edge-agent/terminal-bridge.py` (pty + nsenter spawner), `src/components/TerminalModal.tsx` (xterm.js + WS lifecycle). Auth via `resolve_user_from_cookie(websocket)` on the browser side; bearer cross-check against the session's `device_id` on the agent side. |
| New live-chart shape | The block lives in `src/components/blocks/LiveChartBlock.tsx` and Plotly diffs via `Plotly.react`. The agent emits it via `make_live_chart` — reuse the same `chart_key` to mutate in place. |

ARCHITECTURE.md → "Extension points" has the full version.

---

## Non-obvious gotchas

- **Persistence skips chart data.** `localStorage` holds investigations + blocks; the dataRegistry is in-memory only. Charts in past turns show "data not in cache" after a page reload — this is by design. Don't try to "fix" it by inlining trace data into blocks; that defeats the dataRef pattern.
- **Live charts are mutable; immutable history blocks are not.** A new `live_chart` block with an existing `chart_key` replaces the prior block via `addBlock` in `investigationStore.ts` (scans past turns, strips the old one, appends the new). Older turns lose the chart visually — by design. Don't bolt this onto other block types.
- **`drift-agent/` editable install creates `drift_agent.egg-info/`** — gitignored.
- **Local dev uses two env files**: Vite reads `.env.local` (frontend, `VITE_*` vars); uvicorn reads `drift-agent/.env` (backend). For prod, the only `.env` that matters is `/var/lib/drift-cp/.env` — generated by `deploy/install.sh` and read by `docker compose` via the Software Updates Apply path.
- **CORS is allowlist-based** in `app/main.py` via `ALLOWED_ORIGINS`. Adding a new dev origin? Update the env var.
- **The 20-iteration agent loop cap** in `agent.py` is a real ceiling. Investigations that need more than 20 LLM calls to complete will get truncated. Most finish in 4–8.
- **WS routes can't use FastAPI's `Cookie` dep.** Use `resolve_user_from_cookie(websocket)` in `users/deps.py` for cookie-based auth on WebSocket endpoints. The cookie reads off `websocket.cookies` directly.
- **Children of a flock'd subshell inherit fd 9.** When spawning a long-running background process inside `(flock 9 ... ) 9>"$LOCK_FILE"`, close fd 9 explicitly in the child: `nohup cmd 9>&- &`. Otherwise the child holds the lock for its entire lifetime and the next reconcile tick times out → container restart → child dies. We hit this with the terminal bridge.
- **Self-update only touches `drift-deploy-agent.sh`.** Image-level changes (terminal-bridge.py, python deps, container flags, host-side users) need a fresh `install.sh` run per device. The SHA comparison is on the script alone.
- **Per-user token usage comes from VM, not the in-process registry.** The sidebar uses `sum by (kind) (increase(drift_agent_tokens_total{user="..."}[30d]))` so the number survives drift-agent restarts. In-process counters reset; PromQL `increase()` is counter-reset-safe.

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
