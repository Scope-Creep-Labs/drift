# Drift Deploy — User Guide & Test Scenarios

End-user walkthrough for the v0 of Drift Deploy: how to deploy apps to your devices using Drift's prompt UI. Pair this with [ALERTING.md](./ALERTING.md) (for monitoring deployed apps) and [spec/deploy.md](./spec/deploy.md) (for the full architectural spec).

> **Scope of v0.** Multi-device fleet (4 devices on the current setup), grouped by an operator-chosen `group_id` (`cloud`, `edge`, `drift_home`, …). No Monaco-style file editor yet — you paste compose contents into prompts, but the agent can read existing bundles back with `get_app_revision` so patches don't require re-pasting from scratch. Deploy / fork / delete / group-deploy / query_logs all available as tools. Soft-delete preserves the audit trail.

---

## Quick reference

**Where to drive Drift Deploy from:** https://drift.example.com/drift/ — same prompt UI you use for observability. The agent has 17 deploy tools registered alongside the metrics/alerts/logs tools.

**Fleet:** ask Drift *"list devices and their groups"* — the live answer beats anything in this doc. As of writing, four devices across three groups: `dev-hetzner` (`dev-cloud`), `home-pi4-001` + `home-synology-001` (`drift_home`), and `nvidia-jetson-002` (`dev-work`).

**Blocklist:** `drift-agent`, `drift-postgres`, `drift-frontend`, `drift-deploy-agent` — hard-coded in `PROTECTED_NAMES` in the agent script. Anything else is allowed.

**Quick health checks (curl):**

```bash
# Control plane up + deploy subsystem enabled?
curl -s http://localhost:8000/healthz

# All devices and their statuses
curl -s http://localhost:8000/api/deploy/devices | jq -r '.[] | "\(.name)\t\(.status)\tlast_seen=\(.last_seen)"'

# Deployment targets per device (include tombstones with ?include_removed=true)
curl -s http://localhost:8000/api/deploy/deployments | jq

# Tail the edge agent live (on the device)
docker logs -f drift-deploy-agent
```

---

## How prompts work

The agent follows the same propose-then-apply pattern as alerts: anything that *changes* state (creating a revision, deploying, commissioning) goes through a `propose_*` tool first, the agent shows you the YAML/install command/diff in a `make_markdown` block, you confirm, then the `apply_*` tool runs.

If the agent skips propose and jumps straight to apply, **stop and ask it to show you the proposed state first**. That's the safety rail.

(`fork_app` is the deliberate exception — it does an atomic create + apply of a verbatim copy of another app's latest revision. No propose step because there's nothing for the LLM to paraphrase. See Scenario 4b.)

---

## Edge-agent self-update (v0.5.0+)

The `drift-deploy-agent.sh` script self-updates on every check-in. The control plane includes the current canonical script's 12-char SHA in each `/check-in` response (`agent_target_sha`). When the running agent's SHA differs, the container exits cleanly; Docker's `--restart unless-stopped` brings it back; a bootstrapper at the top of the script fetches `/api/deploy/agent/agent.sh`, `bash -n` checks it, and `exec`s into it. Worst-case downtime per device per update: one poll cycle + container restart, ~20–30s.

What this means in practice: **after the initial `install.sh` on a device, you never need to re-run install just to ship a new agent script.** Push a new `drift-deploy-agent.sh`, rebuild drift-agent, and the fleet picks it up. The in-image baseline is the fallback if the control plane is unreachable at container start.

What it does *not* update: the agent's Docker image (the Dockerfile or alpine baseline). Those still require a one-time re-install per device. In v0 those rarely change.

> **v0.4.0 had a self-update bug** that silently swallowed the exit signal inside the `flock` subshell — agents would log "exiting for Docker restart" every poll cycle without ever actually restarting. v0.5.0 fixes it (exit code 100 sentinel propagated by `main()`). Devices stuck at v0.4.0 need a one-time `docker restart drift-deploy-agent` on the device; after that the bootstrap pulls v0.5.0+ and all future self-updates work.

---

## Apps UI (sidebar create/edit modal)

Creating or editing apps from the chat means pasting multi-file YAML into a textbox while the LLM sits in the data path — easy to fat-finger, easy for the LLM to paraphrase silently. The **APPS** section in the sidebar bypasses all of that:

- **+ New app**: opens a modal with a name field, drag-drop file zone, multi-tab editor (one tab per file), inline rename, delete, "+ Blank". POSTs directly to `/api/deploy/apps` + `/api/deploy/revisions` — no LLM, no paraphrase risk.
- **Click an existing app**: same modal pre-populated from `get_app_revision(app, "latest")`. Save → creates a new revision.

Compose-file presence is validated client-side (the bundle must include `compose.yaml`, `compose.yml`, or `docker-compose.yml`). Each file is capped at 256KB to prevent a stray binary from locking the UI.

The chat surface stays for *operations* (deploy / update / delete / query). Apps are *artifacts* — the modal is the right shape for them.

---

## Retry budget (max_retries)

Every deployment target tracks an `attempts` counter and a `max_retries` cap. Each time the edge agent reports an apply failure on a check-in, the CP increments `attempts`; once it hits `max_retries`, the target flips to status `paused_retries` and the CP stops shipping the bundle. The agent stops retrying because it never sees the desired state for that app anymore.

Where the tracking lives: **CP only.** The edge agent doesn't know about `max_retries` — it just reports failures and acts on what the CP tells it to. Gating happens at the bundle-delivery boundary in the check-in response.

Default cap: 5 attempts. Override per deployment:

> deploy podnot to home-synology-001 with max_retries=10

Tool args: `deploy_revision(app, device, max_retries=10)` or `deploy_revision_to_group(app, group_id, max_retries=10)`. Range 1–100.

After fixing the underlying cause (typo in compose, missing credential, image build), resume with:

> retry podnot on home-synology-001

Tool: `retry_deployment(app, device)` — resets `attempts` to 0 and flips status back to `pending`. Optional `max_retries` to also bump the cap. The agent re-applies on its next check-in.

At fleet scale (100 devices, 5 failures): each device's counter is independent — the 95 healthy targets settle within one tick of the rollout; the 5 failing ones each exhaust their independent budget over `max_retries × POLL_INTERVAL` (≈75s with defaults) then go quiet. `list_deployments` surfaces all 100 with their per-row `attempts/max_retries`; you scan for `paused_retries` rows and resume them once the cause is fixed.

### Behavior when a device goes offline mid-failure

The retry budget counts **real apply attempts that get reported back to the CP**, not wall-clock time and not poll-loop iterations. This matters when devices drop offline:

- **While the device can't reach the CP** — no check-ins succeed, so the CP receives no `apply_errors`. The `attempts` counter is frozen at whatever value it had when the device went dark. The edge agent loops every `POLL_INTERVAL` trying to reach the CP, but **without a desired state from a successful check-in it doesn't have anything to apply** — no `docker compose pull`, no registry hits, no activity at all. The deployment is dormant by absence of instructions, not by an active "pause" state on the CP.
- **When the device comes back online** — first successful check-in reports any deferred `apply_errors` from before the outage; `attempts` increments by one. Apply runs in the same tick (CP has returned a fresh desired list); failure is recorded for the next tick to report. The remaining budget gets consumed at normal cadence (one tick per attempt) until either the apply succeeds or `attempts` hits `max_retries` and the target flips to `paused_retries`.

Concrete example — device fails apply attempts 1 and 2, then loses network for 10 minutes:

| Time | What happens | CP `attempts` |
|---|---|---|
| `t=30s` | Apply 2 fails, reported to CP | 2 |
| `t=45s → t=600s` | Network down. No check-ins. No applies. | 2 (frozen) |
| `t=615s` | Network back. Check-in reports the deferred apply 3 result; applies #4 in the same tick. | 3 |
| `t=630s` | Reports #4; applies #5. | 4 |
| `t=645s` | Reports #5. Cap hit. | `paused_retries 5/5` |

This is the load-bearing reason retry tracking lives on the CP rather than the edge: an edge-side counter would have chewed through the budget during the 10-minute outage, exhausting retries against an unreachable network. CP tracking ensures `max_retries=5` means "5 real apply attempts" — not "5 attempts to do anything."

While the device is offline the CP target shows the last-reported state (`status=failed, attempts=2/5` in the example above). Device freshness is a separate signal — `get_device <name>` shows `last_seen` ageing; a stale `last_seen` with a `failed` deployment is the operator's signal that "this counter isn't moving because the device is gone, not because the cap was hit."

---

## Registry credentials (private images)

Apps whose compose references images on private registries (e.g. `ghcr.io/<you>/*`) need each device's docker daemon to authenticate. Drift handles this end-to-end:

- **Sidebar footer → 🔑 icon** opens the credentials modal.
- Operator enters `registry`, `username`, `password` (a PAT for GHCR). Saved as Fernet ciphertext in Postgres (key = `DRIFT_SECRET_KEY` env var).
- Every agent check-in returns the decrypted creds as a docker `auths` map. The agent writes them atomically to `/root/.docker/config.json` inside its container. From the next `docker compose pull` onwards, private pulls authenticate.
- To rotate: re-paste the new PAT and click Save. Password is never echoed back from the server — every save replaces both fields.
- To revoke: delete the row in the modal. The agents' `config.json` files are *not* automatically rotated; restart the agent container or wait for the next bundle apply.

**Operator setup, once:**
1. Set `DRIFT_SECRET_KEY` in `.env` to a Fernet key (generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`). Rebuild + restart `drift-agent`.
2. Enter credentials in the UI modal.

Without `DRIFT_SECRET_KEY` set, the `/registry-creds` endpoints return 503 and the modal surfaces the disabled state.

> **Threat model:** secrets at rest are encrypted with `DRIFT_SECRET_KEY` (Fernet/AES-128 + HMAC). In transit they ride over Caddy-terminated TLS to the agent. They're decrypted on the CP per check-in (no cache) and written `chmod 600` to the agent container's filesystem. A DB dump alone doesn't expose them; an attacker would also need the key from `.env`.

---

## Releases

drift-agent and the deploy-agent script ship as one image. Whether a release touches the backend Python, the bash script, both, or neither, the release artifact is the same: a tagged `drift-agent` image on GHCR.

**Image:** `ghcr.io/kidproquo/drift-agent`
**Tag scheme:** `vYYYY.MM.DD-<short-sha>` (e.g. `v2026.05.16-cbf703a`). `:latest` is moved when releasing from `main`; feature branches only publish their dated tag.

### Cutting a release (from the build host)

Prereq, once: `docker login ghcr.io -u <gh-username>` with a PAT that has `write:packages`.

```bash
# Make sure HEAD is the commit you want to ship; working tree must be clean.
scripts/release.sh
```

What it does:
1. Builds `drift-agent:vYYYY.MM.DD-<sha>` from `drift-agent/Dockerfile` (which `COPY`s `edge-agent/` into `/opt/edge-agent`).
2. Pushes the dated tag.
3. If `HEAD` is on `main`, retags and pushes `:latest`.

### Consuming a release (on each drift-agent host)

```bash
docker compose pull drift-agent
docker compose up -d drift-agent
```

That's it. Inside the new container, `_agent_target_sha()` computes the SHA of the freshly-baked `drift-deploy-agent.sh`. Every managed device's next check-in sees the new target, exits cleanly, and Docker brings it back on the new script (see "Edge-agent self-update" above).

### Rolling back

Pin a specific dated tag in `docker-compose.yml`:

```yaml
image: ghcr.io/kidproquo/drift-agent:v2026.05.15-<oldsha>
```

`docker compose up -d drift-agent` switches to that image. Fleet picks up the older script's SHA on the next tick. To re-resume tracking `:latest`, change the tag back.

### What is *not* covered by this release flow

- `drift-frontend` and `drift-postgres` still build/pull from their own image lines.
- The image baseline of the edge-agent container on each device (set at `install.sh` time) is unchanged; only the agent's running script self-updates. Image-baseline updates still require a one-time re-run of `install.sh` per device.

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
docker logs --since=2m drift-deploy-agent | grep hello-world
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
> Then deploy v1 to dev-hetzner.

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

### 4a. Patch an existing app without re-pasting the whole bundle

When you want to change one line of a multi-file bundle, ask Drift to pull the current revision first and edit from there.

**Prompt:**

> Show me the current reporter compose, change cadvisor's `housekeeping_interval` from 10s to 30s, and roll a new revision.

**Expected flow:**

1. `get_app_revision(app="reporter")` — returns the latest revision's full file map. The agent shows you the relevant lines in a markdown block.
2. The agent makes the surgical edit and calls `propose_app_revision` with the patched file map. You see the diff implicitly via the changed sha256 and (if the agent renders it) a snippet of the changed lines.
3. You confirm; `apply_app_revision` creates v2.
4. `deploy_revision_to_group(app="reporter", group_id="drift_home")` rolls it out.

**Why this matters:** without `get_app_revision`, you'd have to paste the entire 200-line bundle every time. Even small edits would risk LLM-introduced drift in the unchanged parts.

---

### 4b. Fork an app verbatim (`fork_app`)

Sometimes you want a near-copy of an existing app under a new name — e.g., a `reporter-canary` that's identical to `reporter` for soak testing, or `podnot-staging` mirroring `podnot`. `fork_app` is the one-shot tool for that.

**Prompt:**

> Fork `reporter` to a new app called `reporter-canary` (use the latest revision verbatim).

**What happens under the hood:** `fork_app(src_app="reporter", new_app="reporter-canary")` atomically creates the new app and applies a v1 whose file map is byte-identical to the source's latest revision (same sha256). No `propose_app_revision` round-trip — the verbatim case is safe by construction.

**Then deploy it:**

> Deploy reporter-canary v1 to nvidia-jetson-002 only.

Calls `deploy_revision(app="reporter-canary", device="nvidia-jetson-002")`.

**When to use `fork_app` vs manual create+apply:** use `fork_app` when you want an exact copy. Use the propose/apply pair when you want to change anything (compose, env, config) before the first deploy — the propose flow gives you a sha256-diff against the source.

---

### 4c. Deploy to every device in a group

The fleet-rollout primitive. Works for the initial deploy *and* for rolling out updates.

**Prompt:**

> Deploy reporter v2 to all devices in group drift_home.

Calls `deploy_revision_to_group(app="reporter", group_id="drift_home")`. The response enumerates each device that received an instruction and what action was taken (created, updated, unchanged, or skipped because the device's status disqualified it).

Each device's bash agent reconciles on its own poll cycle (~30s). One slow device doesn't block the others.

**To verify group state:**

> Show me reporter deployment status across drift_home.

Calls `list_deployments(group_id="drift_home")` filtered to `app="reporter"`. You should see one row per device with `current_revision_id == desired_revision_id` and `status=healthy`. Any row stuck at `pending` is your investigation target.

**See [examples/reporter.md](./examples/reporter.md) for the full worked recipe**, including the device-identity injection (`DRIFT_DEVICE_NAME`, `DRIFT_GROUP_ID`, `DRIFT_DOCKER_DATA_DIR`) that lets one bundle adapt per host.

---

### 4d. Query container logs across the fleet (`query_logs`)

The `query_logs` tool runs LogsQL against the central VictoriaLogs instance. Vector on each device forwards container logs (filtered to errors only by default in the reporter bundle) to `https://.../vl/insert/jsonline`. LogsQL is VL's query language — similar to PromQL but for logs.

**Examples:**

> Show me errors from nvidia-jetson-002 in the last hour.

> What containers on the drift_home group have been logging errors most often this week?

> Pull the last 20 log lines from container `vmagent` on home-pi4-001.

The tool returns a compact result the agent can render as a table. Subsequent prompts can drill in (e.g. "the third row — give me the full message").

**Field shape:** logs carry `host`, `group_id`, `container_name`, `image`, and a parsed `level`. LogsQL filter syntax: `host:nvidia-jetson-002 _time:1h level:error`.

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

The flow that lets you stop using Arcane for one app. Worked example: `podnot` — see [examples/podnot.md](./examples/podnot.md) for the full recipe including the registry-credentials setup (the images are on private GHCR) and the named-volume pattern that keeps state portable across devices.

Quick version for the simplest case (no private images, no state separation needed):

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
1. Wait ≤30s for the bash agent to apply the deploy on its next check-in (no per-device allowlist to maintain — the blocklist already protects critical names; everything else is implicitly allowed)
2. `curl https://podnot.princesamuel.me/...` or whatever the public URL is — should work
3. Once you're confident, **delete the podnot project from Arcane** entirely

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

### 8. Tear down an app (soft-delete tombstone)

`delete_deployment` is the supported teardown path. It's a *soft delete*: the deployment target row stays in the database with `status="removed"` and `desired_revision_id=NULL` so the audit trail of "what ran where, and when it was removed" survives. The edge agent sees the null desired-revision, runs `docker compose -p <app> down`, and reports back; the row then transitions from `removing` to `removed`.

**Single device:**

> Remove the `hello-world` deployment from `dev-hetzner`.

The agent should call `delete_deployment(app="hello-world", device="dev-hetzner")`. Within ~30s the next check-in returns an `action="remove"` instruction; the bash agent does `docker compose -p hello-world down`, drops the entry from local `state.json`, and the next check-in confirms removal. Target row's `status` flips `pending → removing → removed`.

**Whole group:**

> Remove reporter from all devices in group drift_home.

Calls `delete_deployment_from_group(app="reporter", group_id="drift_home")`. Returns a per-device action list. Each device's edge agent processes the remove in parallel.

**Listing tombstones:**

By default `list_deployments` hides removed targets. To see them:

> List all deployments including the removed ones.

Calls `list_deployments(include_removed=true)`. Useful for "did anyone deploy X to Y last week?" — the tombstones answer that.

**Re-deploying after removal:**

A tombstoned target is just a row with `desired_revision_id=NULL`. Run `deploy_revision(app=..., device=...)` again and the same row gets its desired-revision set; on the next check-in the device redeploys. No need to delete the tombstone first.

> Note: there is no `delete_app` tool yet — removing an app's *definition* (and all its revisions) requires SQL. Only deployment *targets* have soft-delete in v0.

---

## Worked examples (full recipes)

| File | What it covers |
|---|---|
| [examples/reporter.md](./examples/reporter.md) | Per-host observability stack (vmagent + cAdvisor + node-exporter + Vector) deployed to a group of devices with `deploy_revision_to_group`. |
| [examples/podnot.md](./examples/podnot.md) | Private GHCR images (registry credentials flow end-to-end) + portable state via named volumes. Canonical example of a small real-world app. |

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

- **No `delete_app` tool.** Removing a deployment target is supported (`delete_deployment`, soft-delete tombstone). Removing an app's *definition* (all its revisions, bundle history) still requires SQL — intentional, since this is the destructive case.
- **No rollback button.** To roll back: re-deploy the previous revision (`deploy_revision` with an explicit older `revision_id`). The agent will run the older bundle on the next check-in.
- **No compose-editor block in the UI.** You paste YAML into the prompt textbox. `get_app_revision` lets you patch from current state instead of re-pasting whole bundles. Monaco editor still planned for v1.
- **Blocklist is host-file-level.** Extending the protected name list requires editing `PROTECTED_NAMES` in `drift-deploy-agent.sh`. Rare and intentional; self-update will roll the change to the fleet on the next check-in.
- **No retry backoff** — only a hard cap. The bash agent retries every `POLL_INTERVAL` until the CP's `max_retries` cap is hit (default 5; per-deployment override via `deploy_revision(..., max_retries=N)`). At the cap, status flips to `paused_retries` and the CP stops shipping the bundle. Operator resumes with `retry_deployment(app, device)` (per-device) after fixing the underlying cause. There's no exponential backoff between attempts.
- **Bundle size unbounded.** No upper limit on file size or count in `apply_app_revision`. Don't ship gigabytes of binary blobs in a compose bundle — that's what container images are for.
- **The agent might paraphrase your YAML.** When you paste compose, the LLM is the intermediary. Always check the `propose_app_revision` output against what you intended — the sha256 + file list make this easy to verify. (`fork_app` skips the LLM round-trip entirely for verbatim copies.)

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
| `deploy_revision` returns `pending` and never advances | Bundle's compose hit the blocklist (PROTECTED_NAMES) OR bash agent isn't running | `docker logs --since=2m drift-deploy-agent` on the device; look for `REFUSED` lines |
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
9. **Patch via `get_app_revision`** — Scenario 4a. Verifies the agent fetches the existing bundle instead of asking you to re-paste.
10. **Fork** — Scenario 4b. `fork_app` should skip the propose round-trip and produce a revision whose sha256 matches the source.
11. **Group rollout** — Scenario 4c. Deploy a small app to `drift_home`; verify all three home devices come up healthy on the same revision.
12. **Logs** — Scenario 4d. Crashloop a container and confirm `query_logs` returns the error lines.
13. **Soft-delete** — Scenario 8. Tombstone a deployment, confirm `list_deployments(include_removed=true)` shows it with `status="removed"`.
14. **Self-update** — Change a comment in `edge-agent/drift-deploy-agent.sh`, rebuild drift-agent. Within ~30s every device should log a "self-update available" line and restart on the new SHA. (See commits `99126a1` for the original plumbing and `096f10c` for the subshell-exit fix that made it actually work.)
15. **Registry credentials** — Set fake `example.test` creds in the UI modal. Wait one tick. On any device: `sudo docker exec drift-deploy-agent cat /root/.docker/config.json` should show the auths map. Delete the credential; the file is left as-is (operator can wipe it manually if needed).
16. **Apps UI** — Create an app from the sidebar modal (drag-drop a file, type into a blank tab, save). Verify via chat: *"list apps"* should show it; *"get the latest revision of <app>"* should return the same files you typed.
17. **Retry budget** — Deploy an app with `image: this-image-does-not-exist/nope:v0` and `max_retries=3`. Within ~45s the target reports status=`paused_retries` and attempts=3/3. Stays there. Call `retry_deployment` → attempts back to 0, status=pending, agent retries (and fails again, paused again). Update the image to a real one + `retry_deployment` → succeeds.

Each takes ~5 minutes once you have prompts ready. Drop any failures or surprises into a list and we'll triage.
