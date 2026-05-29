# Drift Deploy — User Guide & Test Scenarios

End-user walkthrough for the v0 of Drift Deploy: how to deploy apps to your devices using Drift's prompt UI. New here? Start at [README.md](./README.md) for the project overview, then come back. Pair this with [ALERTING.md](./ALERTING.md) (for monitoring deployed apps), [ARCHITECTURE.md](./ARCHITECTURE.md) (the agent loop, tool catalog, SSE protocol), and [spec/deploy.md](./spec/deploy.md) (for the full architectural spec).

> **Scope of v0.** Multi-device fleet, grouped by an operator-chosen `group_id` (`cloud`, `edge`, `drift_home`, …) and freely taggable for cross-cutting rollouts (`edge,client-z`). No Monaco-style file editor yet — you paste compose contents into prompts, but the agent can read existing bundles back with `get_app_revision` so patches don't require re-pasting from scratch. Deploy / fork / delete / group-deploy / tag-deploy / query_logs all available as tools. Soft-delete preserves the audit trail.

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

## Authentication and authorization

Drift owns its own login. Caddy basic_auth used to gate `/drift/*` site-wide; that's gone. Anonymous requests to `/api/*` get a 401 from drift-agent's session middleware; the SPA root serves a login page that hits `POST /api/auth/login`.

**Bootstrap:** `DRIFT_ADMIN_USERNAME` and `DRIFT_ADMIN_PASSWORD` in `.env`. The first time drift-agent starts after this is set, it creates an admin user with those creds; subsequent restarts update the password idempotently if it changed. If both vars are unset *and* no admin row exists in the DB, drift-agent logs a clear warning — operator has to set the vars and restart.

**Roles** (one per user, strict containment `observe < deploy < admin`):

| Capability | observe | deploy | admin |
|---|---|---|---|
| Query metrics / logs / alerts | ✓ | ✓ | ✓ |
| Manage alert rules + Alertmanager config | ✓ | ✓ | ✓ |
| Read devices / apps / deployments | ✓ (scoped to their groups) | ✓ (scoped) | ✓ (all) |
| Run investigations (chat) | ✓ | ✓ | ✓ |
| Create apps / propose+apply revisions | ✗ | ✓ | ✓ |
| Deploy / retry / delete deployments | ✗ | ✓ (their groups) | ✓ (any group) |
| Commission / delete devices | ✗ | ✓ | ✓ |
| Manage registry credentials | ✗ | ✓ (their groups) | ✓ (any group) |
| Open web terminal to a device | ✗ | ✓ (their groups, online devices only) | ✓ |
| Manage users + groups | ✗ | ✗ | ✓ |

**Groups:** each user has zero or more device groups (`drift_home`, `dev-cloud`, etc.). Non-admin users only see/act on devices in their groups. Admins always see everything. Apps and revisions are global — anyone can see them, but deploying still requires `deploy` role + group access on the target device.

**Enforcement:** both at the HTTP boundary (FastAPI dependencies on every `/api/deploy/*` endpoint) and inside the LLM tool layer (`_require_deploy_role`, `_check_group_access` at the top of every mutation tool). Defense in depth — a chat user with `observe` role asking the LLM to "deploy podnot" gets a permission-denied response from the tool, not a successful unintended deploy.

**User management** (admin only):
- `POST /api/auth/users` — create with role + groups
- `GET /api/auth/users` — list
- `PATCH /api/auth/users/{username}` — update password / role / groups
- `DELETE /api/auth/users/{username}` — remove

LLM-tool equivalents (admin-only): `list_users`, `create_user`, `set_user_role`, `set_user_groups`, `reset_user_password`, `delete_user`. The create/reset paths return a server-generated password once in the tool response — that text ends up in the chat trace, so hand it to the user out-of-band and clear the investigation afterwards if it's sensitive.

**Self-serve password change** (any role): sidebar footer → 🔁 icon → enter current + new + confirm. POSTs to `/api/auth/me/password`. Verifies the current password server-side; existing sessions are preserved (the user stays logged in afterwards). Min length 8 characters. Cannot reuse the same password. Importantly, **this flow keeps the password off the chat surface entirely** — it never enters the LLM context.

**Login rate limiting.** Both `/api/auth/login` and `/api/auth/me/password` enforce a sliding-window failure counter in two independent buckets: per username (catches slow account-grinding) and per source IP (catches credential stuffing across many usernames from one source). Either bucket hitting its threshold returns `HTTP 429` with a `Retry-After` header and skips bcrypt verify entirely. Successful login clears the username bucket; the IP bucket is never cleared by success so a single correct guess can't reset network-wide enforcement. Tunables in `.env`:

- `LOGIN_MAX_FAILURES_PER_USERNAME` (default 5)
- `LOGIN_MAX_FAILURES_PER_IP` (default 30)
- `LOGIN_FAILURE_WINDOW_SECONDS` (default 900 = 15 min)

State is in-memory on the drift-agent process; restarts reset every counter. Behind a reverse proxy, the limiter prefers the leftmost hop of `X-Forwarded-For` for IP bucketing — Caddy + nginx set this by default in the bundled compose.

Last-admin protection: the system refuses to demote or delete the only admin so it can't lock itself out.

**Session shape:** server-side, in the `sessions` table. Cookie value is an opaque UUID; HttpOnly, SameSite=Lax, Secure in production. 30-day rolling expiry — every authenticated request bumps `expires_at` so active users don't get logged out.

---

## Offline detection

Three layers, all keyed off the same signal — the CP's `device.last_seen` column updated on every successful check-in:

1. **`/api/deploy/devices` status** flips `online → offline` after `DRIFT_DEVICE_STALE_AFTER_SECONDS` of silence (default 300). Runs on the same 30s loop as the observability gauge refresh. As soon as a device starts checking in again, the next successful check-in resets `status=online`.

2. **Prometheus gauge** `drift_deploy_device_last_seen_seconds{device}` exposed at the CP's `/metrics`. Scraped by the `drift-deploy-cp` job in `reporter-cp`'s `prometheus.yml` (deployed only to `dev-hetzner`, where the CP physically runs). One series per device; updated every 30s.

3. **Alert rules** ([examples/alerts/drift-deploy.yml](./examples/alerts/drift-deploy.yml), deployed to vmalert at `/etc/alerts/drift-deploy.yml`):

   - **`DriftAgentStale`** (severity=warning, for=1m) — fires per device when `time() - drift_deploy_device_last_seen_seconds > 300`. Aligned with the CP reaper threshold so the alert and the UI status agree.
   - **`DriftDeployCPMetricsAbsent`** (severity=critical, for=2m) — fires when the gauge itself is absent. Catches "vmagent can't scrape the CP" / "CP /metrics endpoint stopped responding" — distinct from any individual device going stale.

The three layers are designed to be redundant: the CP reaper means the UI tells the truth even if the alert pipeline is broken; the gauge gives an external observability signal; the alert rules turn that into proactive notifications.

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

- **Sidebar footer → 🔑 icon** opens the credentials modal (visible to `deploy` + `admin`).
- Operator enters `registry`, **`group`**, `username`, `password` (a PAT for GHCR). Saved as Fernet ciphertext in Postgres (key = `DRIFT_SECRET_KEY` env var).
- **Group-scoped**: each row is keyed on `(registry, group_id)`. The same registry can have different credentials in different groups (e.g. `ghcr.io` with one account for `cloud`, another for `client-x`). Devices only receive credentials whose `group_id` matches their own at check-in — a compromised token for a `client-x` device cannot read a `client-y` registry secret.
- Every agent check-in returns the decrypted creds for that device's group as a docker `auths` map. The agent writes them atomically to `/root/.docker/config.json` inside its container. From the next `docker compose pull` onwards, private pulls authenticate.
- To rotate: re-paste the new PAT and click Save. Password is never echoed back from the server — every save replaces both fields.
- To revoke: delete the row in the modal. The agents' `config.json` files are *not* automatically rotated; restart the agent container or wait for the next bundle apply.

**Operator setup, once:**
1. Set `DRIFT_SECRET_KEY` in `.env` to a Fernet key (generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`). Rebuild + restart `drift-agent`.
2. Enter credentials in the UI modal.

Without `DRIFT_SECRET_KEY` set, the `/registry-creds` endpoints return 503 and the modal surfaces the disabled state.

> **Threat model:** secrets at rest are encrypted with `DRIFT_SECRET_KEY` (Fernet/AES-128 + HMAC). In transit they ride over Caddy-terminated TLS to the agent. They're decrypted on the CP per check-in (no cache) and written `chmod 600` to the agent container's filesystem. A DB dump alone doesn't expose them; an attacker would also need the key from `.env`.

---

## Web terminal (remote shell)

`deploy` and `admin` users can open an in-browser terminal to any online device in their groups. The flow:

1. **Sidebar Devices section** lists every device the user has access to with a status dot. Click an online row → `TerminalModal` (xterm.js) opens.
2. Browser opens a WebSocket to the CP, CP inserts a `pending` row in `terminal_sessions`, and waits for the agent.
3. Agent picks up `pending_sessions[id]` on its next check-in (≤ POLL_INTERVAL seconds, default 30s) and forks `terminal-bridge.py` — a tiny python helper bundled in the agent image that allocates a pty and execs `nsenter -t 1 -m -p -u -i -- /bin/login` against PID 1 of the host.
4. The user sees a `login:` prompt, types `drift` + the device's drift-user password.
5. PAM authenticates, login execs bash, the user has a host shell. `sudo` works (drift is in the sudoers group) and re-prompts for the same password.

Chat also opens terminals: ask the agent to "open a terminal to dev-hetzner" and it emits a `make_terminal_action` card with an Open button.

**`drift` host user**: provisioned per-device by `install.sh` with a 16-character random password printed once at install. Member of the host's sudoers group (auto-detected: `sudo` / `wheel` / `administrators`). Re-running `install.sh` is idempotent — it leaves the existing password alone, so save the install-time output to a password manager.

**Auth surface**: cookie-authenticated on the browser side, bootstrap-token cross-checked on the agent side. `terminal_sessions` rows record `(user_id, device_id, status, started_at, ended_at, bytes_browser_to_agent, bytes_agent_to_browser)` for audit. **No keystroke capture.**

**Pre-flight guard**: the CP rejects session creation with HTTP 409 (and `last_seen` in the body) when the device isn't online. The sidebar row is greyed out with a tooltip explaining why.

**Container privileges**: install.sh runs the agent container with `--pid host --cap-add SYS_ADMIN --cap-add SYS_PTRACE` so nsenter can enter the host's namespaces. This is a real privilege bump but not a new trust boundary — anyone with the docker socket (which the agent already mounts) is root-equivalent on the host.

**Synology DSM is not supported.** DSM's `/bin/login` exits silently when spawned outside a getty context. The install.sh skips `drift` user creation on DSM and prints a clear warning; use DSM's own SSH for shell access on those devices.

---

## Custom root certificates (corp PKI / TLS-intercepting proxies)

Devices on corp networks often sit behind a TLS-intercepting proxy that re-signs HTTPS traffic with a private CA. Without trusting that CA, the agent's `curl` to the CP and every deployed app's outbound HTTPS will fail with `x509: certificate signed by unknown authority`.

`install.sh` detects the host's combined CA bundle and surfaces it in two places:

1. **Agent container's `curl`** — bind-mounts the bundle at `/host/etc/ssl/host-ca-bundle.crt` and sets `CURL_CA_BUNDLE` in `/etc/drift-deploy/env`. The agent's check-ins inherit it via `--env-file`.
2. **All deployed apps** — exposes the host path as `${DRIFT_HOST_CA_BUNDLE}` in compose subshells. The auto-generated `compose.override.yaml` (per app) sets `SSL_CERT_FILE` + `CURL_CA_BUNDLE` env vars on every service and bind-mounts the bundle at both `/etc/ssl/certs/ca-certificates.crt` (Debian/Ubuntu path) AND `/etc/ssl/cert.pem` (Alpine/BSD path), so apps that read either trust the operator's PKI. Go programs, curl, openssl, and Python's `ssl` module all honor one of these.

Detection order (first match wins): `/etc/ssl/certs/ca-certificates.crt` → `/etc/pki/tls/certs/ca-bundle.crt` → `/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem` → `/etc/ssl/cert.pem`. The host's bundle is what `update-ca-certificates` (or `update-ca-trust`) produces — it already contains both Mozilla roots and any corp CAs the operator added.

Hosts without a recognized bundle file: `install.sh` logs `note: no host CA bundle found at standard locations`, the env var is unset, and both the agent and apps fall back to their container images' built-in Mozilla bundles. This is the right default for non-corp networks.

If a bundle author declares their own volume at one of the standard CA paths, `docker compose up` will refuse to start the service with "duplicate mount point". Resolve by removing the bundle's redundant mount — Drift's injection covers it.

---

## Releases

The drift-agent and drift-frontend images ship under `ghcr.io/kidproquo/drift-agent` and `ghcr.io/kidproquo/drift-frontend`, tagged with semantic versions (`vX.Y.Z`) plus a moving `:latest`. The `drift-deploy-agent.sh` script (run on edge devices) is baked into the drift-agent image and self-updates on every check-in via SHA comparison — so once a release lands on the control plane, the fleet picks up the new script within one poll cycle without any per-device action.

Releases come in two flavors:

- **Image-only** — just code changes (Python in drift-agent, SPA in drift-frontend). Operators apply with one click in the **Software updates** modal in the UI.
- **Bundle** — anything that touches `install.sh`, `docker-compose.yml`, or `config/*.tmpl`. Operators run `curl | tar | install.sh` from the release page on the CP host.

Cutting and consuming releases — including the full update model, version-tracking fields, and all the edge-case scenarios (multiple image-onlys between bundles, mid-update SPA staleness, etc.) — is documented in **[deploy/UPDATES.md](./deploy/UPDATES.md)**. Release authors should read it before cutting a release; operators don't need to (the UI tells them what to do).

The edge-agent container's *image baseline* (Dockerfile, terminal-bridge.py, system packages) does not self-update — image-level changes still require a one-time re-run of `install.sh` per device. The SHA comparison only covers the agent script itself.

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

### 4c-bis. Deploy by tag (cross-cutting rollouts)

Groups are mutually exclusive — a device belongs to exactly one. Tags are not: tag a device with `edge,client-z,low-power` and you can roll out by any of those facets independently. Tag-based deploys use **match-all** semantics: `tags=["edge", "client-z"]` ships to devices that carry both tags.

**Tagging a device:**

> Tag pi-riffpod-001 with edge,client-z.

Calls `tag_device(name="pi-riffpod-001", add=["edge", "client-z"])`. Tags are normalized (lowercased + stripped) and deduped. Remove tags with the same tool: `tag_device(name=..., remove=["client-z"])`. The Sidebar's Devices section also has a chip-style tag editor (sidebar row → tag-edit icon) for non-chat workflows.

**Deploying to a tag set:**

> Deploy reporter-jetson v3 to all devices tagged edge AND client-z.

Calls `deploy_revision_to_tags(app="reporter-jetson", tags=["edge", "client-z"])`. Same response shape as `deploy_revision_to_group` — one row per device that received an instruction, plus a count of how many were skipped (offline, not in user's groups, etc.).

**Why tags are decoupled from `group_id`:** `group_id` is the RBAC + multi-tenancy boundary (per-group registry creds, scoping for non-admin users). Tags are operational labels you change freely without re-thinking authorization. Removing a tag never affects what a device can or can't access; removing it from a group does.

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

**What the agent returns:** a `make_markdown` block with the bootstrap token and a `curl | sudo bash` one-liner. The token is the device's long-lived bearer credential for `/agent/check-in`; it stays valid for the life of the device row in the CP database. Save the curl line to a password manager — the chat won't render it again on later turns, but the value itself remains the right credential for re-installs of the agent on that same device. The token is invalidated when you delete the device from the CP.

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
Same applies to a host rebuild — the saved curl line reinstalls the agent
on a wiped machine and the CP picks it back up on the next check-in,
re-applying any deployed apps from the desired state.

**To re-run install.sh without re-supplying env vars** (for image-baseline
updates where you don't need to rotate creds):

```bash
sudo bash -c 'set -a; . /etc/drift-deploy/env; set +a; \
    unset CURL_CA_BUNDLE SSL_CERT_FILE; \
    curl -fsSL "$CP_URL/agent/install.sh" | bash'
```

Sources the existing `/etc/drift-deploy/env` (mode 600, hence the
`sudo`) to recover `DEVICE_NAME` / `BOOTSTRAP_TOKEN` / `CP_URL` /
`GROUP_ID`. `CURL_CA_BUNDLE` and `SSL_CERT_FILE` get unset because
their values point at container-only paths (`/host/etc/ssl/...`);
install.sh re-detects the host's CA bundle on its own. Useful when
ship an image-baseline change to the edge agent (e.g. v0.1.33's
`/etc/machine-id` bind-mount for fingerprint TOFU) — you want every
device re-installed but don't want to dig out original credentials
from a password manager.

**If you paste the curl on a *different* machine** (intentional migration
or accidental cross-host paste), the new host comes up authenticating as
that device. The CP can't tell the two machines apart — the token is
device-name-scoped, not hardware-bound. Two outcomes:

- *Old machine shut down first.* Clean migration. The new host becomes
  the device; the CP re-applies the desired state on next check-in.
  Useful for hardware swaps.
- *Old machine still running.* Both report in with the same token; the
  CP only has one device row. `last_seen` and reported state flip-flop
  between them on each tick; any deployed app tries to run on both
  hosts simultaneously. Decommission cleanly (`docker stop
  drift-deploy-agent` on the old host, or remove and re-commission the
  device under a new name) before pasting the curl elsewhere.

**Verify on the control plane (from Drift's UI):**

> Did pi-livingroom check in yet?

→ should show status `online`, last_seen within the last 30s.

---

### 6. Migrate an existing stack from Arcane

The flow that lets you stop using whichever tool currently manages your docker-compose stacks (Portainer, Arcane, raw ssh, etc.) for one app at a time.

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
- Image pull: for private images, set up registry credentials via the sidebar 🔑 modal (see "Registry credentials" above). For GHCR public images, no setup needed.

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
| [examples/reporter.md](./examples/reporter.md) | Per-host observability stack (vmagent + cAdvisor + node-exporter + Vector) deployed to a group of devices with `deploy_revision_to_group`. Also documents the full set of `DRIFT_*` builtin vars the agent injects into compose. |

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
