# Worked example: deploy `reporter` to a group of devices

The reporter stack is per-host observability — `vmagent` scrapes a local
`cadvisor` + a host-network `node-exporter` + a local `vector` (log →
metric) and remote-writes everything to the central VictoriaMetrics on
the cloud VM. This recipe ships the same bundle to every device in a
group; each device fills in its own identity at apply time via the
agent's `DRIFT_DEVICE_NAME` / `DRIFT_GROUP_ID` injection.

---

## Prerequisites

1. **Every target device has `drift-deploy-agent` v0.2.0+ running**, with
   `GROUP_ID` set in `/etc/drift-deploy/env`. Verify with:

   ```promql
   count by (version) (drift_deploy_agent_info)
   ```

   If the count of `version="0.2.0"` doesn't match your device count,
   re-run install on the stragglers.

2. **The control plane is reporting `group_id` for each device.** Ask
   Drift: *"list devices and their group_id"*. Empty group means the
   agent is older than v0.2.0 (no check-in field) — re-install.

3. **The central VictoriaMetrics + vmauth are reachable from each
   device** at `https://drift.example.com/vm/api/v1/write` with
   basic auth `reporter:<REDACTED>`. If you're deploying to a
   network that can't reach that URL, this bundle won't work as-is.

---

## Two-stage env-var substitution (why both `${...}` and `%{...}` appear)

The bundle uses two different substitution syntaxes, in two different
stages, expanded by two different runtimes:

| Where | Syntax | Expanded by | When | Source |
|---|---|---|---|---|
| `compose.yaml` env values | `${DRIFT_DEVICE_NAME}`, `${DRIFT_GROUP_ID}` | docker compose | At `compose up` time | The edge agent's shell exports |
| `prometheus.yml` | `%{HOSTNAME}`, `%{GROUP_ID}` | vmagent itself | At config-load time | Env vars on the vmagent container (set by stage 1) |

So `compose.yaml` does:

```yaml
environment:
  HOSTNAME: ${DRIFT_DEVICE_NAME}     # stage 1 → HOSTNAME=home-pi4-001
  GROUP_ID: ${DRIFT_GROUP_ID}        # stage 1 → GROUP_ID=drift_home
```

…and `prometheus.yml` does:

```yaml
external_labels:
  host: %{HOSTNAME}                  # stage 2 → host: home-pi4-001
  group_id: %{GROUP_ID}              # stage 2 → group_id: drift_home
```

`%{...}` is vmagent's own substitution feature, enabled by passing
`-promscrape.config.strictParse=false` in its command list (which the
bundle does). This is the same pattern the hand-managed reporter on
`dev-hetzner` has been using — that's why `host="dev-hetzner"` and
`group_id="dev-cloud"` already show up in VictoriaMetrics queries.

If Drift's agent reviews the bundle and asks *"shouldn't `%{HOSTNAME}`
be `${HOSTNAME}`?"* — the answer is no, leave it.

---

## Step 1 — paste this into Drift to create the app + revision

```text
Create a new app called `reporter`. Apply v1 with these three files
exactly as written (don't reformat). The compose references
${DRIFT_DEVICE_NAME} and ${DRIFT_GROUP_ID} — the agent will fill them
in per device at apply time. The prometheus.yml uses %{HOSTNAME} and
%{GROUP_ID} — that is vmagent's OWN env-substitution syntax (enabled
by --promscrape.config.strictParse=false in the command list), NOT a
typo. Leave the percent-brace form exactly as written.

compose.yaml:
---
services:
  vmagent:
    container_name: vmagent
    hostname: vmagent-${DRIFT_DEVICE_NAME}
    image: victoriametrics/vmagent:latest
    environment:
      HOSTNAME: ${DRIFT_DEVICE_NAME}
      GROUP_ID: ${DRIFT_GROUP_ID}
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
    command:
      - "-promscrape.config=/etc/prometheus/prometheus.yml"
      - "-promscrape.config.strictParse=false"
      - "-remoteWrite.url=https://drift.example.com/vm/api/v1/write"
      - "-remoteWrite.basicAuth.username=reporter"
      - "-remoteWrite.basicAuth.password=<REDACTED>"
    restart: unless-stopped
    extra_hosts:
      - "host.docker.internal:host-gateway"
    depends_on:
      - cadvisor
      - node-exporter
      - vector

  cadvisor:
    image: gcr.io/cadvisor/cadvisor:latest
    container_name: cadvisor
    command:
      - "-housekeeping_interval=10s"
      - "-docker_only=true"
    restart: unless-stopped
    volumes:
      - /:/rootfs:ro
      - /var/run:/var/run:rw
      - /sys:/sys:ro
      - /var/lib/docker/:/var/lib/docker:ro

  vector:
    image: timberio/vector:0.43.0-alpine
    container_name: vector
    hostname: vector-${DRIFT_DEVICE_NAME}
    volumes:
      - ./vector.yaml:/etc/vector/vector.yaml:ro
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - vector-data:/var/lib/vector
    environment:
      VECTOR_CONFIG: /etc/vector/vector.yaml
      VECTOR_DATA_DIR: /var/lib/vector
    restart: unless-stopped

  node-exporter:
    image: prom/node-exporter:latest
    container_name: node-exporter
    network_mode: host
    pid: host
    command:
      - "--path.procfs=/host/proc"
      - "--path.sysfs=/host/sys"
      - "--path.rootfs=/rootfs"
      - "--collector.filesystem.mount-points-exclude=^/(sys|proc|dev|host|etc|var/lib/docker)($$|/)"
      - "--collector.filesystem.fs-types-exclude=^(autofs|binfmt_misc|cgroup|cgroup2|configfs|debugfs|devpts|devtmpfs|fusectl|hugetlbfs|mqueue|nsfs|overlay|proc|procfs|pstore|rpc_pipefs|securityfs|selinuxfs|squashfs|sysfs|tracefs)$$"
      - "--collector.textfile.directory=/var/lib/node_exporter/textfile_collector"
    volumes:
      - /proc:/host/proc:ro
      - /sys:/host/sys:ro
      - /:/rootfs:ro,rslave
      - /var/lib/node_exporter/textfile_collector:/var/lib/node_exporter/textfile_collector:ro
    restart: unless-stopped

volumes:
  vector-data:

---
prometheus.yml:
---
global:
  external_labels:
    host: %{HOSTNAME}
    group_id: %{GROUP_ID}

scrape_configs:
- job_name: cadvisor
  scrape_interval: 60s
  static_configs:
  - targets:
    - cadvisor:8080

- job_name: node
  scrape_interval: 60s
  static_configs:
  - targets:
    - host.docker.internal:9100

- job_name: vector
  scrape_interval: 60s
  static_configs:
  - targets:
    - vector:9598

---
vector.yaml:
---
sources:
  docker:
    type: docker_logs
    docker_host: unix:///var/run/docker.sock
    exclude_containers:
      - vector

transforms:
  classify:
    type: remap
    inputs: [docker]
    source: |
      msg = string!(.message)
      if match(msg, r'(?i)\b(error|exception|panic|traceback|fatal|stack ?trace|critical)\b') {
        .level = "error"
      } else if match(msg, r'(?i)\bwarn(ing)?\b') {
        .level = "warning"
      } else {
        .level = "info"
      }

  to_metrics:
    type: log_to_metric
    inputs: [classify]
    metrics:
      - type: counter
        field: level
        name: container_log_lines_total
        tags:
          container_name: "{{ container_name }}"
          image: "{{ image }}"
          level: "{{ level }}"

sinks:
  prom:
    type: prometheus_exporter
    inputs: [to_metrics]
    address: 0.0.0.0:9598
    flush_period_secs: 600
```

Drift's agent should:

1. Call `create_app(name="reporter")`.
2. Call `propose_app_revision` with the four files. Show the file list +
   sha256 in a `make_markdown` block.
3. Wait for your "ok".
4. Call `apply_app_revision` → bundle packed + uploaded.

---

## Step 2 — deploy to the whole group

```text
Now deploy reporter v1 to all devices in group drift_home.
```

Drift's agent should call `deploy_revision_to_group(app="reporter",
group_id="drift_home")`. The response lists each device and its action
(created/updated) plus any skipped ones. Within ~30s each device's
edge agent reconciles:

- Downloads the bundle from B2 (presigned URL, sha256 verified).
- Extracts to `/var/lib/drift-deploy/apps/reporter/<rev-uuid>/`.
- Generates `compose.override.yaml` injecting `DRIFT_DEVICE_NAME` /
  `DRIFT_GROUP_ID` / `DRIFT_APP` + container labels.
- `docker compose -p reporter pull && up -d --remove-orphans`.
- 30s health probe; if every service reports `running`, target flips
  to `healthy`.

---

## Step 3 — verify

**Per-device deployment status from Drift:**

```text
list deployments
```

Both devices should show `reporter v1 status=healthy`.

**From the central VM** (curl on `dev-hetzner`):

```bash
# All hosts reporting in
curl -s 'http://localhost:8428/api/v1/query?query=up{job=~"cadvisor|node|vector"}' \
  | jq -r '.data.result[] | "\(.metric.host)\t\(.metric.job)\t\(.value[1])"'
```

You should see two new hosts (`home-pi4-001`, `home-synology-001`) with
three jobs each, all `value=1`. The cloud VM continues to report from
the hand-managed reporter.

**Per-group queries:**

```promql
# CPU usage by host within drift_home
avg by (host) (100 - rate(node_cpu_seconds_total{group_id="drift_home", mode="idle"}[5m]) * 100)

# Container error-log rate across drift_home
sum by (host, container_name) (
  rate(container_log_lines_total{group_id="drift_home", level="error"}[5m])
)
```

**Drift labels on the running containers** (run on each device):

```bash
docker inspect vmagent --format '{{json .Config.Labels}}' \
  | jq '. | with_entries(select(.key | startswith("drift.")))'
```

Should print:

```json
{
  "drift.app": "reporter",
  "drift.device_name": "home-pi4-001",
  "drift.group_id": "drift_home",
  "drift.managed": "true",
  "drift.revision": "<uuid>"
}
```

---

## Notes / things to know

- **Synology-specific**: `node-exporter` uses `network_mode: host` and
  `pid: host`. On DSM with Container Manager this works; on older
  Docker-package DSM you may see permission warnings from `node-exporter`
  reading `/proc`. Metrics will mostly still flow.
- **The vmauth password is in the bundle.** Anyone with bundle access
  can extract it. Fine for v0; v1 should swap to `_file` indirection
  like Alertmanager secrets do.
- **Textfile collector**: `/var/lib/node_exporter/textfile_collector` is
  pre-created on each device by `install.sh`. If the agent ever ends
  up on a device where this directory is missing (e.g. cleaned by
  someone), the node-exporter container will fail to start and the
  deployment target will go to `pending` with a clear error.
- **The cloud VM is *not* in this deploy.** It runs the original
  hand-managed reporter at `/root/setup/victoria/reporter/`. If you
  ever want to migrate that off Arcane too, the same bundle works —
  just deploy to `dev-hetzner` (group_id=`dev-cloud`) after stopping
  the hand-managed stack to free the cadvisor/vector container names.

---

## Updating later

To change the reporter compose:

1. *"Update reporter with this new compose:"* + paste the change.
2. Drift calls `propose_app_revision` → shows v2 vs v1 diff.
3. You confirm.
4. *"Roll out v2 to drift_home."* → `deploy_revision_to_group`.
5. Each device picks up v2, runs `compose up -d --remove-orphans` from
   the new bundle dir. Existing containers are recreated in place.

To delete the app from one device only:

```bash
# On the device:
docker compose -p reporter down
# Then in Drift, ask: "remove the reporter deployment from <device-name>"
# (Will need a delete_deployment tool — not in v0 yet; until then,
# remove via SQL on dev-hetzner if needed.)
```
