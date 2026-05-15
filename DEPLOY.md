# Drift Deploy — User Guide & Test Scenarios

End-user walkthrough for the v0 of Drift Deploy: how to deploy apps to your devices using Drift's prompt UI. Pair this with [ALERTING.md](./ALERTING.md) (for monitoring deployed apps) and [spec/deploy.md](./spec/deploy.md) (for the full architectural spec).

> **Scope of v0.** Two-device support (this Hetzner box + planned Pi). No groups. No rollback button. No Monaco-style file editor — you paste compose contents into prompts. Receivers and routes are managed via the alert tools; deploys are managed via the deploy tools.

---

## Quick reference

**Where to drive Drift Deploy from:** https://drift.example.com/drift/ — same prompt UI you use for observability. The agent has 11 deploy tools registered alongside the metrics/alerts tools.

**Currently managed devices:**

| Device | Status | Blocklist (refused) |
|---|---|---|
| `dev-hetzner` | online | `drift-agent`, `drift-postgres`, `drift-frontend`, `drift-deploy-agent` (hard-coded — anything not on the list is allowed) |

**Currently registered apps:**

| App | Latest revision | Notes |
|---|---|---|
| `demo` | v1 | hashicorp/http-echo on `:5678` — used to validate the pipeline |

**Quick health checks (curl):**

```bash
# Control plane up + deploy subsystem enabled?
curl -s http://localhost:8000/healthz

# All devices and their statuses
curl -s http://localhost:8000/api/deploy/devices | jq -r '.[] | "\(.name)\t\(.status)\tlast_seen=\(.last_seen)"'

# Deployment targets per device
curl -s http://localhost:8000/api/deploy/deployments | jq

# Edge-agent's textfile metrics on this host
cat /var/lib/node_exporter/textfile_collector/drift_deploy_agent.prom

# Tail the edge agent live
journalctl -u drift-deploy-agent -f
```

---

## How prompts work

The agent follows the same propose-then-apply pattern as alerts: anything that *changes* state (creating a revision, deploying, commissioning) goes through a `propose_*` tool first, the agent shows you the YAML/install command/diff in a `make_markdown` block, you confirm, then the `apply_*` tool runs.

If the agent skips propose and jumps straight to apply, **stop and ask it to show you the proposed state first**. That's the safety rail.

---

## Scenarios

### 1. Look at your fleet

**Prompt examples:**

> What devices are registered?

> What apps does Drift Deploy manage?

> Show me what's deployed where.

> What's the status of dev-hetzner?

**What the agent does:** calls `list_devices`, `list_apps`, `list_deployments`, or `get_device`. Renders the result as a table or short markdown block.

**Try this combined prompt:**

> Give me a one-screen view of the deploy fleet: every device, its status, and what apps it's running.

The agent should call `list_devices` and `list_deployments`, then assemble two tables.

---

### 2. Deploy a fresh hello-world app

The simplest scenario: spin up a one-container service on dev-hetzner and verify it responds.

**Prompt:**

> Create a new app called `hello-world` with this compose:
>
> ```yaml
> services:
>   echo:
>     image: hashicorp/http-echo:latest
>     command: ["-text=hello from drift deploy"]
>     ports:
>       - "9101:5678"
>     restart: unless-stopped
> ```
>
> Once that's done, deploy it to dev-hetzner.

**Expected flow:**

1. `create_app(name="hello-world")` → `{"app": {...}}`
2. `propose_app_revision(app="hello-world", files={"compose.yaml": "..."})` → preview with sha256
3. The agent shows you the proposed YAML and asks for confirmation
4. You confirm; `apply_app_revision` packs + uploads to B2, returns revision v1 id
5. `deploy_revision(app="hello-world", device="dev-hetzner")` → status `pending`
6. Within 30s of the next check-in, the bash agent pulls the bundle and runs `docker compose up -d`
7. After 30s health probe, status flips to `healthy`

**Verify:**

```bash
curl -s http://localhost:9101            # should print: hello from drift deploy
docker ps --format '{{.Names}}\t{{.Status}}' | grep echo
journalctl -u drift-deploy-agent --since=2min -o cat | grep hello-world
```

---

### The blocklist (bricking safeguard)

The bash agent refuses to deploy any bundle whose compose declares a service name OR a `container_name:` matching one of:

```
drift-agent  drift-postgres  drift-frontend  drift-deploy-agent
```

This is hard-coded in `edge-agent/drift-deploy-agent.sh` (the `PROTECTED_NAMES` array). Anything else is allowed — no per-app allowlist to maintain. If the agent refuses a deploy, the log will read:

```
[<app>] REFUSED: bundle would touch a protected service/container — bricking safeguard
blocklist hit: 'drift-agent' appears in compose as service or container_name
```

The deployment_target stays at `pending` so the failure is visible from Drift. Edit the array in the script if you need to extend the list for a particular host (rare).

---

### 3. Deploy a multi-file app (compose + relative-path config)

When your compose references files via relative paths (e.g. `./prometheus.yml:/etc/prometheus/prometheus.yml`), bundle those files alongside the compose.

**Prompt:**

> Create an app called `tiny-prom` with this layout — three files in the bundle. Use relative paths in the compose.
>
> **compose.yaml:**
> ```yaml
> services:
>   prom:
>     image: prom/prometheus:latest
>     ports:
>       - "9102:9090"
>     volumes:
>       - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro
>     restart: unless-stopped
> ```
>
> **prometheus.yml:**
> ```yaml
> global:
>   scrape_interval: 30s
> scrape_configs:
>   - job_name: self
>     static_configs:
>       - targets: ["localhost:9090"]
> ```
>
> **.env:**
> ```
> # placeholder for future variables
> ```
>
> Then deploy v1 to dev-hetzner. (Already in MANAGED_APPS.)

**Expected:**

- `propose_app_revision` should report `files: ["compose.yaml", "prometheus.yml", ".env"]` and a non-trivial sha256.
- On apply, the bash agent extracts all three files into `/var/lib/drift-deploy/apps/tiny-prom/<rev>/` side-by-side and `docker compose up -d` resolves `./prometheus.yml` against that directory.
- Verify with `curl http://localhost:9102/-/healthy` (Prometheus's own healthcheck).

**Why this matters:** if you put `prometheus.yml` at an absolute host path (`/root/setup/foo/prometheus.yml`), the agent on a different device wouldn't have that file. Relative paths inside the bundle make the app *portable*.

---

### 4. Update a running app

**Prompt:**

> Update hello-world to say "hello from drift v2 — `$(hostname)`" instead. New compose:
>
> ```yaml
> services:
>   echo:
>     image: hashicorp/http-echo:latest
>     command: ["-text=hello from drift v2 - dev-hetzner"]
>     ports:
>       - "9101:5678"
>     restart: unless-stopped
> ```
>
> Then deploy it.

**Expected flow:**

1. `propose_app_revision` — shows v2 vs v1 (same file list, different sha256).
2. You confirm; `apply_app_revision` creates revision v2.
3. `deploy_revision(app="hello-world", device="dev-hetzner")` — sets desired to v2.
4. Within 30s, the agent downloads the new bundle, runs `docker compose up -d --remove-orphans` from the v2 directory. The container is recreated.
5. `curl http://localhost:9101` → `hello from drift v2 - dev-hetzner`.

**Note on data persistence:** since the bundle directory changes per revision (`apps/<app>/<v1>/` → `apps/<app>/<v2>/`), any `./data` bind-mount in the compose would *not* be preserved across revisions. For stateful apps, use absolute paths like `/var/lib/myapp:/app/data` (host paths) — those survive revision changes.

---

### 5. Commission a new device (the Pi)

**Prompt:**

> Commission a new device named `pi-livingroom`.

**What the agent returns:** a `make_markdown` block with the bootstrap token and a `curl | sudo bash` one-liner. Treat the token like a password — it won't be shown again.

**On the Pi (as root):**

```bash
DEVICE_NAME=pi-livingroom \
BOOTSTRAP_TOKEN=drift-…(paste from Drift UI)… \
CP_URL=https://drift.example.com/drift/api/deploy \
curl -fsSL "$CP_URL/agent/install.sh" | sudo -E bash
```

The installer:
1. Drops the env file at `/etc/drift-deploy/env` (chmod 600)
2. Pulls the build context, runs `docker build` to produce
   `drift-deploy-agent:latest` locally (~5s on alpine base)
3. `docker run -d --restart unless-stopped` with the host's docker socket
   and `/var/lib/drift-deploy` bind-mounted

The only host-side dep is Docker itself — no systemd, no jq, no compose
plugin. Same install works on Linux VMs, Raspberry Pi, **Synology NAS**,
anywhere Docker runs.

**Verify on the Pi:**
```bash
docker ps --filter name=drift-deploy-agent
docker logs -f drift-deploy-agent
```

**To upgrade the agent later**: re-run the same `curl … | bash` line.
The installer detects the existing container and replaces it in place.

**Verify on the control plane (from Drift's UI):**

> Did pi-livingroom check in yet?

→ should show status `online`, last_seen within the last 30s.

---

### 6. Migrate an existing stack from Arcane

The flow that lets you stop using Arcane for one app. Worked example: `podnot`.

**Pre-work (out of band):**
- Open Arcane; **stop** the podnot project (don't delete it yet — we want it as a fallback if Drift Deploy has issues). This frees port 32191.
- Decide whether existing state in `/root/dev/podnot/{config,downloads}` should be preserved (yes, in most cases).

**Prompt to Drift:**

> Migrate the podnot service from Arcane to Drift Deploy. The current compose lives at `/root/dev/podnot/docker-compose.yml`. State files are in `/root/dev/podnot/config` and `/root/dev/podnot/downloads` — keep using those exact paths (absolute) so we don't lose state.
>
> Use this compose:
>
> ```yaml
> services:
>   podnot-server:
>     image: ghcr.io/kidproquo/podnot-server:v1.0
>     container_name: podnot-server
>     restart: unless-stopped
>     ports:
>       - "32191:32191"
>     volumes:
>       - /root/dev/podnot/downloads:/app/downloads
>       - /root/dev/podnot/config:/app/config
>
>   podnot-notifier:
>     image: ghcr.io/kidproquo/podnot-notifier:v1.0
>     container_name: podnot-notifier
>     restart: unless-stopped
>     volumes:
>       - /root/dev/podnot/downloads:/app/downloads
>       - /root/dev/podnot/config:/app/config
> ```
>
> Create the app, apply revision, and deploy it to dev-hetzner.

**Post-work:**
1. `sudo sed -i 's/^MANAGED_APPS=.*/MANAGED_APPS=demo,podnot/' /etc/drift-deploy/env && sudo systemctl restart drift-deploy-agent`
2. Wait ≤30s for the bash agent to pick it up
3. `curl https://podnot.princesamuel.me/...` or whatever the public URL is — should work
4. Once you're confident, **delete the podnot project from Arcane** entirely

**Things to watch for:**
- Port conflict: only one of Arcane-podnot or Drift-podnot can bind 32191 at a time. Stop in Arcane FIRST.
- Container name collision: `container_name: podnot-server` is in the compose. If Arcane already has a podnot-server container, `docker compose up -d` will refuse. Stop the Arcane-side container before deploying.
- Image pull: Drift Deploy doesn't manage registry credentials yet (v1). For GHCR public images this is fine; private images need `docker login` to have been run on the device out-of-band.

---

### 7. Check device health from Drift

**Prompts:**

> Is dev-hetzner healthy? When did it last check in?

> Has pi-livingroom checked in in the last 5 minutes?

> Show me the deploy state per device — anything not in `healthy` status?

The agent will call `list_devices` / `get_device` / `list_deployments`. For "healthy in last 5m" it might also use the metrics tools — `device_last_seen_seconds{device="dev-hetzner"}` is exposed on the control plane's `/metrics`.

---

### 8. Tear down an app

The propose/apply pattern doesn't cover deletes yet (v1), but you can:

```bash
# Stop on the device:
sudo docker compose -f /var/lib/drift-deploy/apps/<app>/<rev>/compose.yaml down

# Remove the deployment target so the agent stops trying to maintain it:
curl -u drift:CADDY_PW -X DELETE \
  "https://drift.example.com/drift/api/deploy/devices/<device-name>"   # if you want to remove the device too
```

Or, prompt-driven:

> Delete the hello-world app and its deployment.

The agent today doesn't have a `delete_app` tool — it would have to surface that gap. v1 will add `delete_app` + `delete_deployment`.

---

## Worked examples (full recipes)

| File | What it covers |
|---|---|
| [examples/reporter.md](./examples/reporter.md) | Per-host observability stack (vmagent + cAdvisor + node-exporter + Vector) deployed to a group of devices with `deploy_revision_to_group`. |

## Sample compose files (copy-paste-ready)

### Hello-world (single container, single file)

```yaml
services:
  echo:
    image: hashicorp/http-echo:latest
    command: ["-text=ok"]
    ports:
      - "9999:5678"
    restart: unless-stopped
```

### Tiny prometheus (relative-path config)

**compose.yaml**
```yaml
services:
  prom:
    image: prom/prometheus:latest
    ports:
      - "9102:9090"
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro
    restart: unless-stopped
```

**prometheus.yml**
```yaml
global:
  scrape_interval: 30s
scrape_configs:
  - job_name: self
    static_configs:
      - targets: ["localhost:9090"]
```

### Postgres + adminer (multi-service, env from .env)

**compose.yaml**
```yaml
services:
  db:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_USER: ${PG_USER}
      POSTGRES_PASSWORD: ${PG_PASSWORD}
      POSTGRES_DB: ${PG_DB}
    volumes:
      - /var/lib/test-pg-data:/var/lib/postgresql/data

  adminer:
    image: adminer:latest
    restart: unless-stopped
    ports:
      - "9103:8080"
```

**.env**
```
PG_USER=test
PG_PASSWORD=test
PG_DB=test
```

When you paste a bundle like this to Drift, the agent should set `files = {"compose.yaml": "...", ".env": "..."}` and `apply_app_revision`.

---

## v0 known limitations

- **No groups.** Each deployment targets one device. To deploy the same app to N devices, repeat `deploy_revision` N times.
- **No `delete_app` / `delete_deployment` tools.** Currently you tear down by `docker compose down` on the device or removing the deployment_target row directly. Will come in v1.
- **No registry credentials in the system.** Private images require `docker login` to be done out-of-band on each device.
- **No rollback button.** To roll back: re-deploy the previous revision. The agent will run the older bundle on the next check-in.
- **No compose-editor block in the UI.** You paste YAML into the prompt textbox. Monaco editor planned for v1.
- **Blocklist is host-file-level.** Extending the protected name list requires editing `PROTECTED_NAMES` in the agent script on the device. Fine because it's rare and intentional.
- **Bash agent has no self-update.** A new `agent.sh` requires re-running `install.sh` on the device.
- **No retry budget.** A repeatedly-failing apply will keep retrying every poll. The bash agent logs `apply failed; will retry next tick` but doesn't back off.
- **Bundle size unbounded.** No upper limit on file size or count in `apply_app_revision`. Don't ship gigabytes of binary blobs in a compose bundle — that's what container images are for.
- **The agent might paraphrase your YAML.** When you paste compose, the LLM is the intermediary. Always check the `propose_app_revision` output against what you intended — the sha256 + file list make this easy to verify.

---

## Force a redeploy without bumping the revision

The agent's `state.json` on the device is the source of truth for "what's currently running here". The control plane keeps a `current_revision_id` cache, but the agent's report always wins on the next check-in. So to force a re-apply of the same revision (e.g. after a manual `docker compose down`), you need to clear *both*:

```bash
# On the device:
jq 'del(.current_revisions["<app>"])' /var/lib/drift-deploy/state.json > /tmp/s && mv /tmp/s /var/lib/drift-deploy/state.json

# On the control-plane DB (or via tools later):
docker exec drift-postgres psql -U drift -d drift -c \
  "UPDATE deployment_targets SET current_revision_id=NULL, status='pending' \
   WHERE app_id=(SELECT id FROM apps WHERE name='<app>');"
```

Within ~30s the agent will see drift and re-apply.

## Troubleshooting

| Symptom | Likely cause | Check |
|---|---|---|
| `propose_app_revision` errors with "bundle must contain one of compose.yaml…" | The agent forgot to include the compose filename in the files dict | Tell the agent: "the compose filename must be `compose.yaml`" |
| `apply_app_revision` returns "bundle pack/upload failed" | B2 credentials missing or wrong | `curl localhost:8000/healthz` — `deploy_enabled: true`? Check `B2_*` in .env |
| `deploy_revision` returns `pending` and never advances | Bundle's compose hit the blocklist (PROTECTED_NAMES) OR bash agent isn't running | `journalctl -u drift-deploy-agent --since=2min -o cat`; look for `REFUSED` lines |
| Agent log: `REFUSED: bundle would touch a protected service/container` | Compose declares a service or container_name matching the hard-coded blocklist | Pick a different name; or, deliberately, edit PROTECTED_NAMES in `drift-deploy-agent.sh` |
| Agent log: `sha256 mismatch` | Bundle corrupted in transit (very unlikely) or B2 storing differently | Re-`apply_app_revision`; sha256 will be re-computed |
| Agent log: `post-up health check failed: {...State: "exited"}` | Container crashed within 30s of starting | `docker compose -f /var/lib/drift-deploy/apps/<app>/<rev>/compose.yaml logs` |
| Agent log: `another run holds lock; skipping tick` | Previous tick still applying (e.g. slow image pull) | Normal during big pulls; verify on the next tick |
| `docker logs drift-deploy-agent` shows nothing recent | Container not running, OR env file syntax issue | `docker ps --filter name=drift-deploy-agent`; if it's not there or restarting, `docker inspect drift-deploy-agent` for exit code + check `/etc/drift-deploy/env` syntax |
| Drift UI says "device offline" but agent is running | Control plane unreachable from device, or check-in 401ing | On device: `curl -H "Authorization: Bearer $BOOTSTRAP_TOKEN" $CP_URL/agent/check-in -X POST -H 'Content-Type: application/json' -d '{"device_name":"…","agent_version":"…"}'` |
| `apply` succeeds but the previous version still answers | `docker compose up -d` left the old container running because `container_name` is hard-coded | Either drop the `container_name:` line or use `--remove-orphans` (the agent already does this) |

---

## Where to look in the code

| Concern | File |
|---|---|
| LLM tool handlers + schemas | `drift-agent/app/tools/deploy.py` |
| HTTP admin API | `drift-agent/app/deploy/routes_admin.py` |
| HTTP agent API | `drift-agent/app/deploy/routes_agent.py` |
| Bundle packing + B2 upload | `drift-agent/app/deploy/bundles.py` |
| Database models | `drift-agent/app/deploy/models.py` |
| Edge-agent reconciliation loop | `edge-agent/drift-deploy-agent.sh` |
| Edge-agent installer | `edge-agent/install.sh` |
| Agent container image build | `edge-agent/Dockerfile` |
| Control-plane Prometheus metrics | `drift-agent/app/deploy/observability.py` |
| Auth boundary (Caddy / nginx / app) | this doc's "Sample prompts" → "Migrate an existing stack" section and the auth-fix commit |

---

## Test plan suggestion

If you're systematically validating v0, run these in order. Each builds on the previous and exercises a different code path:

1. **Sanity** — *"List the fleet."* Confirm dev-hetzner shows online, demo deployed and healthy.
2. **Single-file** — Scenario 2 (hello-world).
3. **Multi-file** — Scenario 3 (tiny-prom). Verifies relative-path bundle extraction.
4. **Update** — Scenario 4 (hello-world v2). Verifies revision drift + `--remove-orphans`.
5. **Health probe failure** — Deploy a compose that intentionally crashloops (`command: ["sh", "-c", "exit 1"]`). The agent should report apply failure, deployment_target stays at `pending`, error visible in `last_error` column.
6. **Blocklist** — Try to deploy a bundle whose compose has `services.drift-agent:` (or any other protected name). Agent log should say `REFUSED: bundle would touch a protected service/container`; target stays `pending` forever (this is correct).
7. **Multi-device** — Scenario 5 (commission the Pi).
8. **Real migration** — Scenario 6 (podnot).

Each can take 5 minutes once you have prompts ready. Drop any failures or surprises into a list and we'll triage.
