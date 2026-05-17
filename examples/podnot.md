# Worked example: deploy `podnot` (private GHCR images + portable state)

`podnot` is a podcast notifier + downloader I run at home — two
containers (`podnot-server`, `podnot-notifier`) built from a private
`ghcr.io/kidproquo/*` repo. It's the canonical small-but-real example
for two Drift Deploy features at once:

- **Registry credentials** — the GHCR images are private, so every
  device's docker daemon needs a token to pull them.
- **Named volumes for state** — the app mixes config (RSS feeds, ntfy
  settings) with state (download history, log files) in the same
  directory. To stay portable across devices, the bundle ships *config*
  and Docker manages *state* in named volumes.

If you don't have podnot specifically, the recipe shape generalizes to
any private-registry app whose container co-locates seed config with
generated state.

---

## Prerequisites

1. **Every target device on agent v0.5.0+.** The credential plumbing
   (`/root/.docker/config.json` written per check-in) shipped in 0.5.0.
   Verify:

   ```promql
   count by (version) (drift_deploy_agent_info)
   ```

   Anything below 0.5.0 cannot pull from a private registry until you
   manually `docker login` on that device.

2. **A GitHub PAT with `read:packages`.** Generate at
   github.com → Settings → Developer settings → Personal access tokens.
   Drift never writes this anywhere outside encrypted DB storage.

3. **Drift's secrets subsystem enabled** (i.e. `DRIFT_SECRET_KEY` set
   in `.env`, see the "Secrets" block in `.env.example`). If it isn't,
   `/api/deploy/registry-creds` returns 503 and the credentials modal
   surfaces the error.

---

## Step 1 — register the GHCR credential (once)

UI: sidebar footer → 🔑 icon → **Registry credentials** modal opens.

```
Registry:  ghcr.io
Username:  kidproquo
Password:  ghp_xxxxxxxxxxxxxxxxxxxx   ← PAT with read:packages
```

Click **Save**. Within ~30s every agent in the fleet picks up the
auth on its next check-in and writes it into its own
`/root/.docker/config.json`. From that point on, `docker compose pull
ghcr.io/kidproquo/*` works on every device.

The password is never echoed back from the server — to rotate, you
re-enter the new PAT and click Save again.

**Verify** (optional, from inside an agent container on any device):

```bash
ssh <device>
sudo docker exec drift-deploy-agent cat /root/.docker/config.json
# → {"auths":{"ghcr.io":{"auth":"<base64-of-user:pat>"}}}
```

---

## Step 2 — create the `podnot` app

Sidebar → **APPS** → **+ New app**. Two tabs in the modal:

**Tab 1: `compose.yaml`**

```yaml
services:
  podnot-server:
    image: ghcr.io/kidproquo/podnot-server:v1.0
    restart: unless-stopped
    ports:
      - "32191:32191"
    volumes:
      - podnot-config:/app/config
      - podnot-downloads:/app/downloads
      - ./config.json:/app/config/podcast_notifier/config.json:ro

  podnot-notifier:
    image: ghcr.io/kidproquo/podnot-notifier:v1.0
    restart: unless-stopped
    volumes:
      - podnot-config:/app/config
      - podnot-downloads:/app/downloads
      - ./config.json:/app/config/podcast_notifier/config.json:ro

volumes:
  podnot-config:
  podnot-downloads:
```

**Tab 2: `config.json`** — the RSS feed list + ntfy settings. Anything
the operator wants podnot to read at boot. Example:

```json
{
  "podcasts": [
    { "name": "Self-Hosted",   "url": "https://selfhosted.show/rss" },
    { "name": "Vergecast",     "url": "https://feeds.megaphone.fm/vergecast" }
  ],
  "ntfy": {
    "server":  "https://ntfy.sh",
    "topic":   "kidproquo-podnot-Tz7K6Q",
    "priority": "default"
  },
  "download": {
    "enabled":     true,
    "directory":   "./downloads/podcasts",
    "server_url":  "https://drift.example.com/podnot",
    "server_port": 32191
  },
  "check_interval": 3600
}
```

Click **Create v1**. The modal POSTs directly to `/api/deploy/apps` +
`/api/deploy/revisions` — the LLM never sees the YAML, so it can't
paraphrase it.

---

## Step 3 — deploy

In the chat:

> deploy podnot to home-synology-001

The agent calls `deploy_revision(app="podnot",
device="home-synology-001")`. Within ~30s:

1. Synology's edge agent receives `desired = [{app: podnot, action:
   deploy, bundle_url: ..., sha256: ...}]` plus the GHCR auth from
   step 1.
2. Agent downloads + verifies the bundle, extracts to
   `/var/lib/drift-deploy/apps/podnot/<rev-uuid>/`.
3. Agent generates a `compose.override.yaml` injecting the device facts
   (`DRIFT_DEVICE_NAME`, `DRIFT_GROUP_ID`, `DRIFT_APP`).
4. `docker compose -p podnot pull` — succeeds now, with GHCR creds in
   place. Both `podnot-server` and `podnot-notifier` images pulled.
5. `docker compose -p podnot up -d --remove-orphans`. Both containers
   start.
6. 30s health probe — both report `running`.
7. Target flips to `healthy`. `current_revision_id` ← desired.

---

## Step 4 — verify

In the chat:

> List deployments

Should show `podnot v1 status=healthy` on home-synology-001.

On the device itself:

```bash
ssh home-synology-001
# Both containers up?
sudo docker ps --filter "label=drift.app=podnot" \
  --format '{{.Names}}\t{{.Status}}\t{{.Image}}'

# Server responding?
curl -I http://localhost:32191/

# Volumes created with the right state?
sudo docker volume ls --filter "name=podnot"
sudo docker exec podnot-podnot-server-1 ls /app/config/podcast_notifier
# → config.json (seeded from bundle), plus podcast_data.json + log files
#   that the running app has started creating in the volume
```

If you have Caddy routing on that device (e.g. you've added a
`reverse_proxy localhost:32191` block for a `podnot.<your-domain>`
host), the public URL also works.

---

## How the state separation actually works

The line that's doing all the work in the compose:

```yaml
volumes:
  - podnot-config:/app/config                            # state (named volume)
  - ./config.json:/app/config/podcast_notifier/config.json:ro   # config (overlay)
```

Docker layers the file mount on top of the volume mount. From inside
the container:

```
/app/config/                                 ← named volume "podnot-config" (rw)
/app/config/podcast_notifier/config.json     ← bundled file (ro, overrides what's in volume)
/app/config/podcast_notifier/podcast_data.json  ← app writes here → goes into volume
/app/config/podcast_notifier/*.log              ← app writes here → goes into volume
```

| Layer | Where | Survives revisions? | Survives device move? |
|---|---|---|---|
| `compose.yaml`, `config.json` | bundle dir | new dir per revision | N/A — re-uploaded |
| Running state | `podnot-config` named volume | ✓ yes | no (volume is per-device) |
| Downloaded mp3s | `podnot-downloads` named volume | ✓ yes | no |

When you ship `config.json` v2 (e.g. a new feed added):

1. Drift packs the new bundle (compose + config.json).
2. Synology's agent downloads + extracts to a new revision dir.
3. The named volume `podnot-config` keeps its content (history, logs).
4. The config.json overlay points to the new revision's file → app
   reads the new feeds on next start.
5. `docker compose -p podnot up -d --remove-orphans` recreates the
   containers; volumes stay attached.

When you deploy to a new device for the first time:

1. Agent creates the named volumes (they start empty).
2. `compose up` starts containers; podnot writes `podcast_data.json`
   into the empty volume, slowly rebuilds the "what's been seen"
   history over the first `check_interval` cycles (default 3600s).
3. New downloads accumulate in `podnot-downloads`.

There's no manual rsync. The cost of portability is that each device
starts with a blank history.

---

## Updating later

### Add a podcast

> Open the podnot app, add this feed to config.json, and ship v2:
> name = "Risky Business", url = "https://risky.biz/feeds/risky-business"

The agent will call `get_app_revision(app="podnot")` for the current
config, splice in the new feed, then propose the new revision. Or you
can do it manually:

1. Sidebar → APPS → click **podnot** → modal opens pre-populated.
2. Switch to the `config.json` tab, edit the JSON.
3. Click **Save as new revision**.
4. Chat: *"deploy podnot v2 to home-synology-001"* (or whichever
   devices have it).

### Roll back

`deploy_revision(app="podnot", revision_id="<older>")` — supply the
older revision's UUID (visible via `list_app_revisions`). The agent
ships the older bundle; the named volumes survive, so running state
is unaffected.

### Remove from a device

> Remove podnot from home-synology-001.

Soft delete (the audit trail stays). Agent stops + removes the
project; named volumes are *not* automatically wiped (Docker
behavior — operator decides when to `docker volume rm podnot-config
podnot-downloads`).

---

## Notes / things to know

- **The GHCR PAT is the most sensitive secret in this whole flow.**
  It's encrypted at rest with `DRIFT_SECRET_KEY` and travels per
  check-in over Caddy-terminated TLS. Rotate the PAT in GitHub,
  re-enter it in the Drift modal, hit Save — every agent picks up
  the new value within 30s.

- **Port 32191 must be free on every target device.** If you want to
  deploy to two devices in the same group, give each a different host
  port (`32191:32191` vs `32192:32191`) by shipping per-device
  revisions, OR scope deployment to one device.

- **`container_name:` is deliberately absent** from the compose.
  Drift runs `docker compose -p podnot up`, which prefixes containers
  as `podnot-podnot-server-1` / `podnot-podnot-notifier-1`. A hard-
  coded `container_name:` would conflict with the project namespacing.

- **No `.env` file in this bundle.** Podnot reads everything from
  `config.json`. If you have an app that needs operator-managed env
  vars (Slack webhooks, API tokens) the registry-creds modal isn't
  the right surface — that's the env-var-secrets piece, which is a
  future Drift feature.

- **First-deploy bandwidth cost.** Both podnot images are ~150MB
  combined. On a Raspberry Pi over WiFi, pulling them for the first
  time takes ~30s. Subsequent revisions reuse cached layers; only the
  app-layer diff downloads.

## Failure modes for this app specifically

### "denied" on first deploy → credentials missing or wrong

If you deploy podnot to a device before registering the GHCR credential
(or after rotating the PAT without re-saving), the device will start
retrying and report:

```
last_error=Error response from daemon: Head "https://ghcr.io/v2/...": denied
```

With the default `max_retries=5` and the device's 15s poll interval, the
deployment exhausts its retry budget after ~75s and flips to
`paused_retries 5/5`. Resolution:

1. Confirm via `list_deployments` that the only error is auth-denied.
2. Open the credentials modal (sidebar → 🔑), re-enter `ghcr.io` + GH
   user + PAT, click Save.
3. Wait one tick (~15s) for every agent to write its
   `/root/.docker/config.json`.
4. Resume the paused target: *"retry podnot on home-synology-001"*.
5. Next tick: agent re-applies, pull succeeds, containers come up
   healthy.

### Device drops offline mid-deploy

If the device's network dies (WiFi blip, router reboot) partway through
the retry budget, **the CP counter freezes at its last-reported value**.
The agent loops trying to reach the CP every 15s but does *not* attempt
any apply while it has no desired state in hand — no registry hits, no
log spam during the outage. When connectivity returns, the deferred
report is delivered on the first successful check-in and the remaining
budget plays out at normal cadence. See DEPLOY.md → "Retry budget" →
"Behavior when a device goes offline mid-failure" for the full trace.

### Pull keeps failing on one specific device only

If 3 of 4 devices succeed and one keeps failing past `max_retries`, the
diagnosis usually splits into:
- **Network-path differences**: that one device can't reach
  `ghcr.io` (try `ssh <device> sudo docker pull alpine:3.20` — works
  cleanly? then it's not a general network issue).
- **Docker daemon storage exhausted**: `ssh <device> df -h
  /var/lib/docker` or `/volume1/@docker` on Synology. Pulls fail
  cryptically when there's no disk for the new layers.
- **The credentials never reached this specific device**: `ssh
  <device> sudo docker exec drift-deploy-agent cat
  /root/.docker/config.json` — should show the auths map. If empty,
  the agent might be stuck at v0.4.0 (pre-credential support); check
  `agent_version` for that device with `list_devices`.
