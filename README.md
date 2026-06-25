# Drift

**Observe, deploy, respond. From a prompt.**

Drift is a prompt-driven control plane for time-series systems and edge fleets. You ask questions or give instructions in plain language; an LLM agent picks the right tools, queries your VictoriaMetrics / Prometheus, runs statistical analysis, ships compose bundles to your devices, manages alert rules, and assembles a rich response — markdown, charts, tables, metric cards, timelines — that streams progressively into the UI as the work unfolds.

```
prompt → agent (tool use → metrics / fleet / alerts) → streaming render blocks → UI
```
## Observe
<img width="1499" height="736" alt="image" src="https://github.com/user-attachments/assets/f000ccbe-e8d6-4d7a-8c51-12089d492db8" />

## Query
<img width="1492" height="869" alt="image" src="https://github.com/user-attachments/assets/368b04c2-de48-43dd-9b5e-a432ed5430a2" />
<img width="1499" height="867" alt="image" src="https://github.com/user-attachments/assets/f75a4f1b-0ea7-4059-951e-ca6a80c27d30" />

## Deploy
<img width="926" height="884" alt="image" src="https://github.com/user-attachments/assets/c79bc3c5-6152-472f-a51b-ce52404c9683" />





[Blog post](https://scopecreeplabs.com/blog/drift-observe-deploy-respond---from-a-prompt/) with details. 

> 📐 [ARCHITECTURE.md](./ARCHITECTURE.md) — data flow, dataRef pattern, agent loop, tool catalog, extension points, file reference.
>
> 🚀 [DEPLOY.md](./DEPLOY.md) — Drift Deploy: fleet management, compose-app delivery, scenarios.
>
> 🚨 [ALERTING.md](./ALERTING.md) — vmalert + Alertmanager + the agent's 14 alert tools, end-to-end workflows.
>
> 📦 [deploy/README.md](./deploy/README.md) — single-server bundle (VM stack + Drift CP + Caddy/TLS on one box) with a guided installer.

---

## Install

The fast path is the single-server bundle. One Linux host with Docker, one public domain, two minutes of prompts:

```bash
VERSION=v0.1.41
curl -L "https://github.com/Scope-Creep-Labs/drift/releases/download/${VERSION}/drift-deploy-${VERSION#v}.tar.gz" | tar -xz
cd "drift-deploy-${VERSION#v}"
./install.sh
```

`install.sh` pulls `ghcr.io/kidproquo/drift-agent:latest` and `drift-frontend:latest`, so a fresh install lands directly on the current image versions regardless of which bundle tag you used. See [deploy/README.md](./deploy/README.md) for the full operator walkthrough (DNS, prompts, day-2 ops) and [deploy/UPDATES.md](./deploy/UPDATES.md) for the bundle-vs-image-only release model.

Want to hack on the code instead? See [Quickstart](#quickstart) below.

---

## What you can do

Three pillars, all driven from the same chat. The agent uses ~30 tools across them; you don't pick the tools, you describe the goal.

### 🔍 Observe — investigate what's happening

Ask anything about your telemetry. The agent discovers what metrics exist, picks the right query, fetches the data (which never enters the LLM context — see [the dataRef pattern](./ARCHITECTURE.md#the-dataref-pattern)), runs statistics, and assembles a streamed response with charts, tables, summaries, and timelines.

```text
> Which hosts are reporting metrics, and what jobs are scraping?
> Show CPU usage on the host over the last 15 minutes.
> Which containers are using the most memory right now?
> Look for anomalies in network traffic over the last hour.
> Compare p95 request latency between dev-cloud and edge devices last week.
> Plot disk I/O on jetson-002 every 5 seconds.        ← live chart
> Now change the refresh rate to 1s.                  ← mutates the same chart in place
> Pull the last 200 error lines from dev-hetzner.     ← log search via VictoriaLogs
```

Outputs: streaming markdown narration, Plotly charts, sortable tables, metric cards with sparkline trends, event timelines, live-refreshing charts.

### 🚀 Deploy — manage your fleet

Drift Deploy registers each device with a small edge agent that polls the control plane every 30s, applies whatever compose bundles you've assigned, and reports back. You drive the whole thing from the same prompt UI — devices, apps, revisions, tagging, deploy-by-tag, rollback. RBAC + per-group scoping keeps non-admins out of devices that aren't theirs. See [DEPLOY.md](./DEPLOY.md) for the full scenario catalog.

```text
> List devices and their groups.
> Show what's deployed to home-pi4-001 right now.
> Tag pi-riffpod-001 with edge, client-z.
> Fork the reporter app as reporter-jetson.
> Save a new revision of reporter-jetson — here's the compose: <paste>
> Deploy reporter-jetson v3 to all devices tagged edge AND client-z.
> Roll home-pi4-001 back to reporter v2.
> Pull last 50 lines of the edge agent on dev-hetzner.
```

Outputs: propose-then-apply diffs in markdown, deployment status timelines, retry/conflict surfaces, terminal-action blocks, archive downloads (`.tar.gz` / `.zip`) of any revision.

### 🛎️ Respond — close the loop

Investigations end in action. From the same chat, manage vmalert rules and Alertmanager routing, silence noise during planned work, or jump straight into a host shell. The agent uses the same propose-then-apply pattern as deploys so you see exactly what will change before it does. See [ALERTING.md](./ALERTING.md) for the alert subsystem details.

```text
> List firing alerts.
> Create an alert when CPU > 90% for 5 minutes on any edge device.
> Silence anything from jetson-002 for 2 hours — I'm rebooting it.
> Wire up a webhook so critical alerts ping https://ntfy.sh/drift-alerts.
> Show the receivers configured in alertmanager.
> Open a terminal to home-pi4-001.                    ← xterm.js, one click in the UI
```

Outputs: propose-then-apply rule/receiver diffs, alert state timelines, terminal-action blocks, and the in-browser terminal modal (full pty, mux-friendly with `TERM=xterm-256color`, audited per session).

---

## Motivation

I wanted to observe and deploy docker-compose stacks across a fleet of Linux hosts — homelab, edge, cloud, corp — from one place, conversationally. The constraints came first; the architecture fell out of them.

- **No inbound ports on target devices.** Edge agents poll out to the control plane every 30s; nothing listens on the device side. Works behind NAT, firewalls, residential routers, and corp networks without holepunching, port forwards, or VPNs.
- **No SSH after the first install.** Once the device is commissioned (one `curl | bash` over SSH), everything happens through the CP: deploys, updates, tag changes, log queries, and even shell access (in-browser via xterm.js, audited per session). The agent script self-updates from the CP via SHA comparison on each check-in — no per-device upgrade chore. Image-baseline changes are the one exception and remain a deliberate, infrequent per-device step.
- **Queue-based deploys, not push.** Desired state lives on the CP. Targets can be offline when you make a change — when they come back, they converge. No imperative "ssh-and-run" model that breaks when half the fleet is asleep.
- **Compose is the contract.** Apps are versioned bundles of plain files (`compose.yaml` + `.env` + configs). If `docker compose up` runs it on your laptop, Drift can ship it. Rollback is "deploy revision v2" — no proprietary packaging, no special tooling.
- **Groups and tags for dynamic filtering.** Groups are the RBAC/multi-tenant boundary (one per device); tags are free-form operational labels (`edge`, `client-z`, `low-power`) that overlap freely. Match-all rollouts (`deploy to tags=["edge","client-z"]`) handle the cross-cutting cases that groups alone can't.
- **Lean on the proven observability stack.** VictoriaMetrics + VictoriaLogs + vmalert + Alertmanager + Grafana + node-exporter + cAdvisor + Vector — lightweight, replaceable, no homegrown protocols. Drift builds the *interaction layer*, not another TSDB.
- **PromQL as the query language.** The agent generates and runs PromQL; the operator never has to see it. Anything that speaks the Prometheus query API plugs in (VM, Prometheus, Thanos, Mimir).
- **Tool calling to extend the agent, not fine-tuning.** New capability = a function in `app/tools/*.py` plus a JSON schema. No retraining, no embeddings store, no RAG. Telemetry data flows through tools and stays out of the LLM context (the [dataRef pattern](./ARCHITECTURE.md#the-dataref-pattern)) — analysis is precise (numpy/scipy actually computes); the model orchestrates. Stops the "LLM hallucinated a p95" failure mode and keeps token cost flat regardless of fleet size.
- **Propose-then-apply for every mutation.** The LLM never silently changes state. Creating an alert rule, deploying a bundle, editing a route — each goes through a `propose_*` tool that surfaces the diff before `apply_*` runs. This is how you let an LLM touch production.
- **Watch the investigation, not just the answer.** Tool calls, narration, intermediate charts, results — all painted progressively as the agent works. No 30-second blank wait followed by a wall of text. Trust comes from seeing how the result was reached.
- **Self-hosted, self-owned.** One Caddy + the Drift CP + a TSDB on a single Linux box. Your devices, your data, your model key. No SaaS phone-home, no per-device subscription, no vendor.
- **Bring-your-own model.** Claude Opus 4.7 is the default for its quality on agentic loops, but `MODEL=…` + the engine adapter pattern let you point at Sonnet, Haiku, or anything else. The frontend doesn't know which model is running.
- **RBAC + per-group scoping out of the box.** Three roles (`observe < deploy < admin`), per-user group membership scopes which devices a user can see/touch, separate registry credentials per group. Multi-tenant from day one rather than retrofit.
- **Host-CA injection for corp networks.** `install.sh` detects the host's combined CA bundle and propagates it to the agent plus every deployed app (mounted at the standard Debian + Alpine paths, plus `SSL_CERT_FILE` / `CURL_CA_BUNDLE` in env). Ship to devices sitting behind a TLS-intercepting corp proxy without per-app workarounds.

The same constraints rule out a lot of common shapes: no PaaS-style "give us your code", no per-device daemon you upgrade by hand, no log-aggregator-as-a-service, no "let the LLM read all your data" RAG, no listening sockets on target devices.

---

## What the LLM sees (and doesn't)

The agent operates on metadata — names, labels, summaries, configs by reference — not on raw secrets or raw bulk data. The boundary is enforced in code, not by prompting the model to behave.

**What the LLM has access to:**

- Names and metadata: metric / label / job names, device names + groups + tags + statuses, app / revision metadata, alert rule names + expressions + labels, receiver names + webhook URLs, session metadata.
- File contents of compose bundles when explicitly fetched via `get_app_revision` — typically `${VAR}` references; the actual values come from device-side env.
- Time-series *summaries* (n, mean, p50, p95, min, max, …) computed server-side from each query. Raw arrays stay server-side under a `prom://<uuid>` dataRef and are pushed straight to the UI via SSE (the [dataRef pattern](./ARCHITECTURE.md#the-dataref-pattern)).
- Log lines returned by `query_logs` — the same content you'd see in `docker logs` on the device.

**What the LLM never has access to:**

- API keys (`ANTHROPIC_API_KEY`, etc.) and any other env-var credentials — env vars don't enter the prompt or the tool-result surface.
- Drift's database password (`DRIFT_PG_PASSWORD`) and Fernet key (`DRIFT_SECRET_KEY`).
- Auth secrets for the TSDB / vmalert / Alertmanager (`VM_BASIC_AUTH`, `VMALERT_BASIC_AUTH`, `ALERTMANAGER_BASIC_AUTH`, etc.) — tool handlers attach these directly to outbound `httpx` calls.
- Registry credentials — encrypted at rest with `DRIFT_SECRET_KEY`, decrypted only per device check-in, shipped over TLS straight to the edge agent. Operators set them via a UI modal that bypasses the LLM entirely.
- Alertmanager receiver secrets (bearer tokens, webhook auth) — the agent only calls `Path.exists()` on `am-secrets/*` filenames and emits a *path reference* (`bearer_token_file: /etc/alertmanager/secrets/<name>`). Alertmanager opens the file at notify time; the LLM never sees the bytes.
- Raw time-series arrays — kept under server-side dataRefs, streamed to the UI out-of-band.
- Web-terminal bytes — pty stdio flows agent ↔ edge over a dedicated WebSocket and never the LLM.
- User passwords — set + verify happen server-side via `passlib`; the LLM has no read path to the password column.

**Three places where sensitive content briefly touches the chat surface:**

- `create_user` / `reset_user_password` return a server-generated password ONCE in the tool response, which renders into the chat trace. Hand it to the user out-of-band and clear the investigation afterwards. The self-service "change my password" sidebar flow keeps the new password off the chat entirely.
- `commission_device` returns a one-shot bootstrap token in the curl line it generates. The token is single-use — once a device claims it, it's exhausted — and acts as a device-commissioning credential, not a long-lived secret.
- If you paste compose contents with literal secrets in `.env` into the prompt, the LLM sees what you typed. Use `${VAR}` references resolved on the device, or the registry-credentials modal for image-pull tokens — both keep secrets off the chat.

---

## What you get

- **Frontend** — React + Material UI dark theme, Plotly time-series charts, real-time streaming UI that surfaces the agent's thinking and tool calls. Sidebar lists devices and apps in your groups; xterm.js opens a host shell in one click.
- **Backend** — FastAPI agent powered by Claude Opus 4.7 (default; configurable via `MODEL`) with adaptive thinking, prompt caching, and ~30 tools across discovery / query / analysis / fleet / alerts / render-block emission.
- **Multi-user RBAC** — login + cookie sessions, three roles (`observe` < `deploy` < `admin`), user-group scoping for devices, audit log for terminal sessions. Bootstrap an admin via env vars; manage the rest from chat or the admin API.
- **Drift Deploy** — promote a compose bundle as an "app", push to one device or every device matching a tag set, watch the edge agents reconcile in real time. Per-group registry credentials, edge-agent self-update, retry budgets, conflict detection, host-CA injection for corp PKI.
- **Live charts** — `make_live_chart` polls a server-side PromQL passthrough on a timer and `Plotly.react`-diffs in place; mutating one keeps zoom/hover.
- **Compose stack** — slim Docker images for both services. Brings its own TSDB? No — point it at any Prometheus-compatible source via `VM_URL`. The bundled single-server install adds VictoriaMetrics, VictoriaLogs, vmalert, Alertmanager, Grafana, and Caddy/TLS.

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

> On this host, a VM stack lives at `/root/setup/victoria/` (single-node VM on `:8428`, vmauth basic-auth proxy on `:8427`, Grafana on `:3000`) with a vmagent + cadvisor reporter at `/root/setup/victoria/reporter/`. `deploy/install.sh` walks you through pointing Drift at it via the public vmauth URL.

---

## Quickstart

Two paths, pick whichever fits.

### Option A — single-server install (full stack)

The supported full-stack install path is `deploy/install.sh`. It generates `/var/lib/drift-cp/.env` with random secrets, renders config templates, brings up the compose stack from `deploy/docker-compose.yml`, and (optionally) issues TLS via a bundled Caddy. See [DEPLOY.md](./DEPLOY.md) for the walk-through.

```bash
git clone <this repo>
cd drift/deploy
sudo ./install.sh
```

Try:

- *"Which hosts are reporting metrics, and what jobs are scraping?"*
- *"Show CPU usage on the host over the last 15 minutes."*
- *"Which containers are using the most memory right now?"*
- *"Look for anomalies in network traffic over the last hour."*

For VM cluster (vmselect): set `VM_TENANT_PATH=/select/<accountID>/prometheus`. For auth: set `VM_BASIC_AUTH=user:pass` or `VM_BEARER_TOKEN=...`. Both go in `/var/lib/drift-cp/.env` after install.

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

For a **full-stack install**, env vars live in `/var/lib/drift-cp/.env` — generated by `deploy/install.sh` and read by `docker compose` (including the in-app **Software Updates → Apply** path). Edit there and trigger an apply to take effect.

For **local dev**:

- **`drift-agent/.env`** — read by uvicorn (backend).
- **`.env.local`** at the repo root — read by Vite (frontend; `VITE_*` only).

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
| `ALLOWED_ORIGINS`    | no       | `http://localhost:5173,http://127.0.0.1:5173` | Comma-separated CORS allowlist for the FastAPI app. `install.sh` sets this to the frontend origin. |

### Frontend env vars

| Variable              | Default               | Notes                                                   |
| --------------------- | --------------------- | ------------------------------------------------------- |
| `VITE_ENGINE`         | `mock`                | `agent` for the real backend, `mock` for synthetic.     |
| `VITE_API_BASE`       | `/api`                | Base URL the AgentAdapter POSTs to.                     |
| `VITE_AGENT_DEV_URL`  | `http://localhost:8000` | Where Vite's dev proxy forwards `/api/*`. Dev only.   |

The frontend image is built with `VITE_ENGINE=agent` and `VITE_API_BASE=/api` (via build args in the Dockerfile). Override at build time with `--build-arg VITE_ENGINE=mock` if you want the UI without the backend.

---

## Project structure

Top level:

```
drift/
├── README.md                  this file
├── ARCHITECTURE.md            deep dive: data flow, agent loop, dataRef pattern, tool catalog
├── ALERTING.md                vmalert + Alertmanager subsystem; alert/silence/receiver tools
├── DEPLOY.md                  Drift Deploy user guide; deploy/commission/migrate scenarios
├── deploy/                    install.sh + docker-compose.yml + config templates for the full stack
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

- **Persistence**: investigation history is in `localStorage` under key `drift.investigations.v2`. Chart trace data is in-memory only — see [ARCHITECTURE.md → The dataRef pattern](./ARCHITECTURE.md#the-dataref-pattern). User auth, devices, apps, registry creds, terminal session metadata live in Postgres (the `drift-postgres` service in compose). Token usage is reported as metrics into VictoriaMetrics so the sidebar's per-user "$X used" survives drift-agent restarts.
- **Agent loop cap**: the loop is bounded at 20 LLM iterations; most investigations finish in 4–8.
- **No automated tests yet.** Verification is manual via the UI.
- **Bootstrap admin**: set `DRIFT_ADMIN_USERNAME` + `DRIFT_ADMIN_PASSWORD` in `.env` for first-run admin creation. Subsequent users are created from chat or the admin API.

---

## License

Drift is licensed under the [Apache License 2.0](./LICENSE). Copyright 2026 Scope Creep Labs LLC.

## Contributing

Contributions are welcome — bug fixes, features, docs, edge-agent ports to new platforms. See **[CONTRIBUTING.md](./CONTRIBUTING.md)** for the development setup and PR guidelines.

All contributors must sign the **[Individual Contributor License Agreement](./CLA.md)**. Our CLA Assistant bot posts a one-click signing link on your first pull request; sign once and it covers every future PR. The CLA permits Scope Creep Labs LLC to relicense future versions of the project under different terms — Apache 2.0 on existing releases is permanent.

For security reports, please email **support@scopecreeplabs.com** rather than opening a public issue.
