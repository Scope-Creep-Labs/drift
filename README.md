# Drift

**Agentic observability for time-series systems.** Ask questions about your telemetry in plain language; an LLM agent picks the right tools, queries your VictoriaMetrics / Prometheus, runs statistical analysis, and assembles a rich response — markdown, charts, tables, metric cards, timelines — that streams progressively into the UI as the investigation unfolds.

> 📐 For the full architecture deep dive (data flow, dataRef pattern, agent loop, tool catalog, extension points, file reference), see [ARCHITECTURE.md](./ARCHITECTURE.md).
>
> 🚨 For the alerting subsystem (vmalert + Alertmanager + the agent's 14 alert tools, with end-to-end workflows), see [ALERTING.md](./ALERTING.md).

---

## What you get

- **Frontend** — React + Material UI dark theme, Plotly time-series charts, real-time streaming UI that surfaces the agent's thinking and tool calls.
- **Backend** — FastAPI agent powered by Claude Opus 4.7 with adaptive thinking, prompt caching, and 16 tools across discovery / query / analysis / render-block emission.
- **Compose stack** — slim Docker images for both services. Brings its own TSDB? No — point it at any Prometheus-compatible source via `VM_URL`.

```
prompt → agent (tool use → metrics fetch → analysis) → streaming render blocks → UI
```

---

## Prerequisites

| Tool         | Version  | Why                                            |
| ------------ | -------- | ---------------------------------------------- |
| Docker       | ≥ 24.0   | Recommended path for running everything.       |
| Docker Compose | ≥ 2.20 | Bundled with Docker Desktop.                   |
| Node.js      | ≥ 20     | Local frontend dev (alternative to Docker).    |
| Python       | ≥ 3.12   | Local backend dev (alternative to Docker).     |
| Anthropic API key | — | Required for the agent to actually call the LLM. Get one at https://console.anthropic.com. |

You also need a **Prometheus-compatible time-series source** the agent can reach:
- Your VictoriaMetrics (single-node or vmselect cluster) via `VM_URL`.
- Any Prometheus-API-compatible store (Prometheus, Thanos, Grafana Mimir, etc.).

> On this host, a VM stack lives at `/root/setup/victoria/` (single-node VM on `:8428`, vmauth basic-auth proxy on `:8427`, Grafana on `:3000`) with a vmagent + cadvisor reporter at `/root/setup/victoria/reporter/`. The shipped `.env.example` shows how to point Drift at it via the public vmauth URL.

---

## Quickstart

Two paths, pick whichever fits.

### Option A — Docker

```bash
git clone <this repo>
cd drift
cp .env.example .env
$EDITOR .env       # ANTHROPIC_API_KEY plus VM_URL (and VM_BASIC_AUTH / VM_BEARER_TOKEN if needed)
docker compose up --build
```

The frontend is exposed on host port `10001` (mapped to nginx :80 in the container). Open <http://localhost:10001> for direct access, or wire it up behind a reverse proxy at the path of your choice (this repo's deployment is at <https://drift.example.com/drift/>). Try:

- *"Which hosts are reporting metrics, and what jobs are scraping?"*
- *"Show CPU usage on the host over the last 15 minutes."*
- *"Which containers are using the most memory right now?"*
- *"Look for anomalies in network traffic over the last hour."*

For VM cluster (vmselect): set `VM_TENANT_PATH=/select/<accountID>/prometheus`. For auth: set `VM_BASIC_AUTH=user:pass` or `VM_BEARER_TOKEN=...`.

If your VM is on the **docker host** (not in another compose stack on a shared network), use `VM_URL=http://host.docker.internal:8428` and add `extra_hosts: ["host.docker.internal:host-gateway"]` to the `drift-agent` service.

### Option B — local dev (no Docker)

Run the backend in a venv and the frontend in Vite's dev server. Best for iterating on code.

**Backend:**

```bash
cd drift-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env       # set ANTHROPIC_API_KEY + VM_URL (must be reachable from your machine)
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

**Frontend (in another terminal):**

```bash
npm install
cat > .env.local <<EOF
VITE_ENGINE=agent
VITE_AGENT_DEV_URL=http://localhost:8000
EOF
npm run dev
```

Open <http://localhost:5173>. Vite's dev proxy forwards `/api/*` to the backend, so the same code path works in dev as in Docker.

To iterate on the UI without spending API credits or running a backend, set `VITE_ENGINE=mock` instead. The frontend ships 5 hard-coded scenarios with synthetic data.

---

## Configuration

All env vars live in two files:

- **Root `.env`** — read by `docker-compose.yml` and substituted into both services.
- **`drift-agent/.env`** — read by the agent when running locally with `uvicorn` (outside Docker).

In Docker, only the root `.env` matters. In local dev, both can exist independently — the frontend reads `drift/.env.local`, the agent reads `drift-agent/.env`.

### Agent env vars

| Variable             | Required | Default                                  | Notes                                      |
| -------------------- | -------- | ---------------------------------------- | ------------------------------------------ |
| `ANTHROPIC_API_KEY`  | yes      | —                                        | Claude API key.                            |
| `VM_URL`             | yes      | —                                        | Base URL of your VictoriaMetrics / Prometheus. |
| `VM_TENANT_PATH`     | no       | `""`                                     | `/select/<id>/prometheus` for vmselect; empty for single-node. |
| `VM_BASIC_AUTH`      | no       | `""`                                     | `user:pass`. Sent as `Authorization: Basic`. |
| `VM_BEARER_TOKEN`    | no       | `""`                                     | Sent as `Authorization: Bearer <token>`.    |
| `MODEL`              | no       | `claude-opus-4-7`                        | Any current Claude model ID.               |
| `EFFORT`             | no       | `high`                                   | `low / medium / high / xhigh / max`.       |
| `MAX_TOKENS`         | no       | `64000`                                  | Per-iteration `max_tokens`.                |
| `ALLOWED_ORIGINS`    | no       | `http://localhost:5173,http://127.0.0.1:5173` | Comma-separated CORS allowlist for the FastAPI app. Compose sets this to the frontend origin. |

### Frontend env vars

| Variable              | Default               | Notes                                                   |
| --------------------- | --------------------- | ------------------------------------------------------- |
| `VITE_ENGINE`         | `mock`                | `agent` for the real backend, `mock` for synthetic.     |
| `VITE_API_BASE`       | `/api`                | Base URL the AgentAdapter POSTs to.                     |
| `VITE_AGENT_DEV_URL`  | `http://localhost:8000` | Where Vite's dev proxy forwards `/api/*`. Dev only.   |

In Docker, the frontend image is built with `VITE_ENGINE=agent` and `VITE_API_BASE=/api` (via build args in the Dockerfile). Override at build time with `--build-arg VITE_ENGINE=mock` if you want the UI without the backend.

---

## Project structure

Top level:

```
drift/
├── README.md                  this file
├── ARCHITECTURE.md            deep dive: data flow, agent loop, dataRef pattern, tool catalog
├── ALERTING.md                vmalert + Alertmanager subsystem; alert/silence/receiver tools
├── docker-compose.yml         frontend + agent
├── Dockerfile                 frontend: alpine node builder + nginx alpine runtime
├── nginx.conf                 SPA + SSE-friendly /api proxy
├── package.json               frontend dependencies
├── tsconfig.json
├── vite.config.ts             dev proxy /api → VITE_AGENT_DEV_URL
├── index.html
├── src/                       React frontend
├── drift-agent/               Python backend (FastAPI + agent + tools)
└── spec/                      original product specs (reference only)
```

For a full file-by-file breakdown, see [ARCHITECTURE.md → File reference](./ARCHITECTURE.md#file-reference).

---

## Common dev tasks

### Add a new tool the agent can call

Edit one of `drift-agent/app/tools/{metrics,analysis,emit}.py`:

1. Define an `async def my_tool(ctx, args)` returning a JSON-serializable dict.
2. Add an entry to that file's `*_TOOLS` list (JSON Schema describing inputs).
3. Register the handler in `*_HANDLERS`.

The agent picks it up automatically on next request — system prompt and tools list rebuild from the registries on import.

See [ARCHITECTURE.md → Extension points](./ARCHITECTURE.md#extension-points).

### Add a new render block type

1. Add the variant to `src/types/blocks.ts` and `drift-agent/app/schemas.py`.
2. Write a React component under `src/components/blocks/`.
3. Register it in `src/components/blocks/BlockRenderer.tsx`.
4. Add an emit tool in `drift-agent/app/tools/emit.py`.

### Iterate on the UI without burning tokens

Set `VITE_ENGINE=mock` in `.env.local`. The Mock adapter synthesizes a fake event stream from 5 hard-coded scenarios (`gateway-17 instability`, `fleet thermal`, `dispatch optimization`, `v2.8 regression`, `latency correlation`). The streaming UI works the same.

### Type-check and build

```bash
# Frontend
npx tsc --noEmit
npm run build

# Backend
cd drift-agent && .venv/bin/python -c "from app.main import app; print('OK')"
```

There are no automated tests yet — verification is manual end-to-end via the UI.

### Switch to a different LLM model

Set `MODEL=claude-sonnet-4-6` (or any current Claude ID) in `.env`. Adjust `EFFORT` for the cost/quality balance you want. Restart the agent.

To use a different LLM provider entirely, refactor `drift-agent/app/agent.py:run_agent`. The SSE protocol stays the same, so no frontend changes are needed.

---

## Troubleshooting

**Agent fails to start with "1 validation error for Settings: anthropic_api_key".**
You haven't set `ANTHROPIC_API_KEY` in `.env`. The Settings class requires it.

**Agent starts but `/investigate` returns an error: "anthropic_api_error: ...".**
Either the key is invalid, the model ID is wrong, or you've hit a rate limit. Check the agent's logs (`docker compose logs drift-agent` or the uvicorn terminal).

**Agent runs but every tool call fails with HTTP timeout / connection refused.**
The agent can't reach `VM_URL`. From inside the agent container: `docker compose exec drift-agent curl -s "$VM_URL/api/v1/labels"`. Common causes:
- VM is on the docker host but `VM_URL=http://localhost:8428` — containers can't see the host's `localhost`. Use `http://host.docker.internal:8428` with an `extra_hosts: host-gateway` mapping, or attach drift to the VM stack's docker network.
- Auth required but `VM_BASIC_AUTH` / `VM_BEARER_TOKEN` not set (e.g. you're hitting `vmauth` on `:8427`, not `vm:8428`).
- Firewall / Tailscale not connected.
- vmselect cluster but you forgot `VM_TENANT_PATH=/select/0/prometheus`.

**Agent fetches data but charts in the UI show "Chart data is no longer in cache".**
You reloaded the page. The dataRegistry is in-memory only — re-run the prompt to refetch.

**`cache_read_input_tokens` shows 0 across consecutive turns.**
Something invalidated the prompt cache prefix. Look in `drift-agent/app/agent.py` for non-deterministic content in `SYSTEM_PROMPT` or the tools list (timestamps, UUIDs, varying tool order). The prefix must be byte-stable across calls.

**"Failed to load chart data: dataRef not found: prom://..."**
The agent emitted a chart referencing a ref that wasn't pushed via a `data` event. Check the agent logs; usually means an emit tool fired before the underlying `query_range` succeeded. File a bug.

**Vite dev server won't start with port-in-use error.**
`lsof -ti:5173 | xargs kill` to free the port.

**Docker build fails on `npm ci`.**
Delete `node_modules/` locally before building (Docker's COPY may have picked up a partial install).

**Frontend serves but `/api/*` returns 502 in nginx.**
The agent container isn't healthy. `docker compose ps` should show it `running` and `healthy`. If not, `docker compose logs drift-agent`.

**Agent runs slowly / takes 30+ seconds.**
Normal for complex investigations — `claude-opus-4-7` with `effort=high` is thorough. Lower `EFFORT=medium` if you need faster, less exhaustive responses.

---

## Notes

- **Persistence**: investigation history is in `localStorage` under key `drift.investigations.v2`. Chart trace data is in-memory only — see [ARCHITECTURE.md → The dataRef pattern](./ARCHITECTURE.md#the-dataref-pattern).
- **Agent loop cap**: the loop is bounded at 20 LLM iterations; most investigations finish in 4–8.
- **No automated tests yet.** Verification is manual via the UI.
- **No multi-user / auth.** Single-user demo. Adding auth would require a state model with sessions and a backing DB.

---

## License

Not yet specified. Treat as proprietary until added.
