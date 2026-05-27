# Alerting — vmalert + Alertmanager + Drift tools

Drift can manage time-series alerts conversationally: creating rules, silencing noise, wiring up webhook receivers, and routing matchers to receivers. This doc covers the system end-to-end so you can extend it without re-discovering the moving parts.

New here? Start at [README.md](./README.md) for the project overview. For the underlying agent architecture (SSE protocol, tool dispatch, dataRef pattern, system prompt), see [ARCHITECTURE.md](./ARCHITECTURE.md); for the sibling fleet-management pillar, see [DEPLOY.md](./DEPLOY.md).

---

## Architecture

```
                    ┌─────────────────────────────┐
                    │   Drift agent (LLM + tools) │
                    └──────────────┬──────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        │                          │                          │
        │ rules                    │ config + reload          │ silences / api
        ▼                          ▼                          ▼
  drift-managed.yml         alertmanager.yml             alertmanager api
  (rw bind-mount)           (rw bind-mount)              (https + basic auth)
        │                          │                          │
        │                          │                          │
        ▼                          ▼                          ▼
  ┌──────────┐               ┌──────────┐               ┌──────────┐
  │ vmalert  │──notifier──── │   AM     │──notify────── │  ntfy /  │
  │  :8880   │               │  :9093   │               │ webhook  │
  └──────────┘               └──────────┘               └──────────┘
        │                          ▲
        │ datasource/remoteWrite   │ reads via /run/secrets-style
        ▼                          │   indirection (no inline secrets)
  ┌──────────┐               ┌──────────┐
  │   VM     │               │ secrets/ │  (ro mount; agent never reads)
  │  :8428   │               └──────────┘
  └──────────┘
```

Two compose stacks involved:

- **`/root/setup/victoria/`** owns `vm`, `vmalert`, `alertmanager`, `vmauth`, `grafana`.
- **`/root/dev/drift/`** owns `drift-agent` + `drift-frontend`. The agent reaches vmalert/AM through Caddy at `drift.example.com/vmalert` and `/am` with basic auth, and edits rule/AM-config files via bind-mounts of the VM stack's paths.

The split keeps observability infra ops-team-owned and Drift's agent/UI app-team-owned. Drift writes through filesystem bind-mounts; the only thing it controls in the LLM context is *which file*, *which key*, *which value* — never secret contents.

---

## File layout

On the host (paths the agent writes to are bind-mounted into drift-agent):

| Host path | Mode | Mounted into drift-agent at | Who edits |
|---|---|---|---|
| `/root/setup/victoria/alerts/starter.yml` | ro to agent | `/etc/alerts/starter.yml` | **You** (hand-curated) |
| `/root/setup/victoria/alerts/drift-managed.yml` | **rw** to agent | `/etc/alerts/drift-managed.yml` | **Agent only** |
| `/root/setup/victoria/alertmanager.yml` | **rw** to agent | `/etc/alertmanager/alertmanager.yml` | Agent (idempotent upserts by receiver/route name) |
| `/root/setup/victoria/am-secrets/<name>` | ro to agent | `/etc/alertmanager/secrets/<name>` | **You** (drop bearer tokens, passwords, sensitive URLs) |

Ownership:

- `am-secrets/` and `alertmanager.yml` are owned by uid 999 (matches drift-agent's `app` user) with `o+r` so the AM container (uid 65534) can also read.
- `alerts/` likewise — vmalert reads via the `internal` network namespace.

This sealed perms model relies on `/root` being mode 700 — non-root users on the host can't enter `/root/setup/...` regardless of inner perms.

---

## The tools

Fourteen tools, grouped by capability. Source: `drift-agent/app/tools/alerts.py`. Schemas registered in `agent.py:all_tools()`.

### Read-only

| Tool | Hits | Returns |
|---|---|---|
| `list_alert_rules` | vmalert `/api/v1/rules` | groups + rules, with state (firing/pending/inactive), labels, annotations |
| `list_active_alerts` | vmalert `/api/v1/alerts` | currently firing or pending alerts (optionally filtered by state) |
| `list_silences` | AM `/api/v2/silences` | active silences (set `include_expired=true` for history) |
| `list_receivers` | AM `/api/v2/receivers` | receiver names |

### Rule lifecycle (vmalert)

| Tool | Side effect | Notes |
|---|---|---|
| `propose_alert_rule` | — | Pure preview. Returns the YAML the agent would write and whether it's a create or update. Use BEFORE `apply_alert_rule`. |
| `apply_alert_rule` | writes `drift-managed.yml`, hits vmalert `/-/reload` | Idempotent by alert name. Never touches `starter.yml` or other hand-edited files. Atomic write (tmp + rename). |
| `delete_alert_rule` | rewrites `drift-managed.yml`, reloads | Refuses if the rule isn't in `drift-managed.yml` (hand-managed rules must be removed manually). |

### Silencing (Alertmanager)

| Tool | Side effect | Notes |
|---|---|---|
| `silence_alert` | POST AM `/api/v2/silences` | Takes matchers + duration (`1h`, `7d`, etc.). Default duration 1h. |
| `delete_silence` | DELETE AM `/api/v2/silence/{id}` | Expires a silence by uuid. |

### Receivers (Alertmanager)

Only webhook-style receivers are supported (covers ntfy + generic webhook + Discord + similar). Slack/SMTP/PagerDuty are out of scope by design — the agent shouldn't carry their secret schemas.

| Tool | Side effect | Notes |
|---|---|---|
| `propose_receiver` | — | Preview a webhook receiver. Warns if any referenced secret file is missing. |
| `upsert_receiver` | writes `alertmanager.yml`, hits AM `/-/reload` | Idempotent by receiver name. Always references secrets BY FILENAME (the agent never sees the actual value). |
| `delete_receiver` | rewrites, reloads | Refuses if a top-level route still references the receiver. |

### Routing (Alertmanager)

| Tool | Side effect | Notes |
|---|---|---|
| `set_route` | writes top-level route in `alertmanager.yml`, reloads | One route per receiver in the top-level `route.routes` array. Matchers are converted to AM's string form (`severity="critical"`, `host=~"pi.*"`, …). |
| `delete_route` | rewrites, reloads | Removes the top-level route targeting a given receiver. |

---

## The propose/apply pattern

For every mutation that touches a config file, the agent **must** call `propose_*` first, surface the YAML to you in a `make_markdown` block, and wait for your explicit OK before calling `apply_*` / `upsert_*`. This is enforced via the system prompt; if you see the agent skipping straight to apply, tighten the wording in `agent.py:SYSTEM_PROMPT` step 4.

Why this matters:

- vmalert's PromQL is easy to get subtly wrong (label names, aggregation arity). The propose step gives you a chance to spot it.
- An incorrect receiver block or matcher can silently misroute every alert. Same.
- The agent only sees label values you've already exposed in your TSDB — it can't validate intent.

---

## Workflows

### Create an alert rule

1. *"Create an alert for when the cloud VM's root disk drops below 15% free for 30 minutes."*
2. Agent: calls `list_metrics`/`list_hosts` to confirm `node_filesystem_avail_bytes` is available with a matching `host` label; calls `propose_alert_rule`; emits the proposed YAML.
3. You: "looks good, apply."
4. Agent: calls `apply_alert_rule` → writes `drift-managed.yml`, POSTs `/-/reload`.

### Silence a noisy alert

1. *"Silence the InstanceDown alert for the Pi for 2 hours — I'm rebooting it."*
2. Agent: optionally calls `list_active_alerts` to find the right labels; proposes the silence in prose; on confirm calls `silence_alert` with `matchers: [{name: alertname, value: InstanceDown}, {name: host, value: pi-livingroom}]`, `duration: 2h`.
3. Agent reports the silence id; the user can later say "remove the silence on the Pi" → agent finds it via `list_silences` and calls `delete_silence`.

### Wire up an ntfy receiver

Two-step: out-of-band secret + Drift's tools.

```bash
# 1. Drop the secret on the host (file name is your choice; the agent will reference it).
echo -n 'tk_yourbearer1234567890' > /root/setup/victoria/am-secrets/ntfy-default
chmod 644 /root/setup/victoria/am-secrets/ntfy-default
```

Then in Drift:

> *"Set up an ntfy receiver named `ntfy-default` pointing at https://ntfy.example.com/drift-alerts, using bearer auth with credentials file `ntfy-default`. Route any critical alert there."*

The agent:

1. Calls `propose_receiver` → renders the YAML block (verifies the secret file exists).
2. On your confirm, `upsert_receiver` → writes + reloads AM.
3. `set_route` with `matchers: [{name: severity, value: critical}]` → AM now routes anything labeled `severity="critical"` to ntfy.

To test end-to-end without waiting for a real alert, temporarily add a trivial firing rule like `expr: vector(1)` `for: 0s` with `labels.severity: critical`.

### Replace a receiver's URL

> *"Update ntfy-default to point at a new topic: https://ntfy.example.com/drift-alerts-v2"*

Agent calls `propose_receiver` → `upsert_receiver` (same name, different URL). Idempotent — the existing block is replaced, the route binding stays intact (routes are receiver-name keyed).

### Tear down

> *"Remove the ntfy receiver and its route."*

Order matters: routes reference receivers. Agent calls `delete_route` first, then `delete_receiver`. AM rejects deleting a referenced receiver, and the tool surfaces that as a clear error if the agent gets the order wrong.

---

## Operational notes

### Reload semantics

- vmalert: `POST {VMALERT_URL}/-/reload`. Reloads rule files glob; on a parse error vmalert returns 4xx and the *previous* rules stay loaded. Tools propagate the reload error back so the agent can tell the user "the file was written but vmalert refused it — fix and retry."
- Alertmanager: `POST {ALERTMANAGER_URL}/-/reload`. Same behavior — bad config → 4xx, previous config stays effective.

### Atomic writes

`_save_managed()` and `_save_am_config()` both write to a `.tmp` file in the same directory and `os.replace()` into place. POSIX rename is atomic, so an inotify watcher (vmalert / AM) never sees a half-written file.

### Why the agent can't see secrets

`/etc/alertmanager/secrets` is mounted read-only into drift-agent. The agent only calls `Path(...).exists()` on filenames — it never opens the files. The LLM context never receives the secret. The agent generates a path *reference* (`/etc/alertmanager/secrets/ntfy-default`); AM (which has its own read-only mount of the same dir) is the one that actually opens and reads the file at notification time.

If you want belt-and-suspenders, drop the secrets dir mount from drift-agent entirely. The tools will then write filename references blindly, AM will fail at notify time if a file is missing, and the agent has zero filesystem access to the secrets domain. The current setup is preferred because the existence-check gives you a real-time warning at propose-time rather than at the moment of a missed page.

### What's NOT managed by the agent

Deliberately:

- Slack / PagerDuty / SMTP / Opsgenie receivers (their secret schemas vary; the agent's webhook-only surface is the minimum sufficient for ntfy + most homelab notification setups).
- Nested route trees (`route.routes[*].routes[*]`). Only the flat top-level `route.routes` is touched.
- Global AM options (`global.resolve_timeout`, `smtp_smarthost`, …) — change those by hand in `alertmanager.yml`.
- `inhibit_rules` — left to hand editing.
- Files other than `drift-managed.yml` under the rules glob.

If you need any of these, edit `alertmanager.yml` / the rules files directly and just `docker exec` a reload (`curl -X POST localhost:9093/am/-/reload`).

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Tool returns `VMALERT_URL not configured` / `ALERTMANAGER_URL not configured` | `.env` missing the var; rebuild drift-agent after editing. |
| Tool returns `HTTP 401` | `VMALERT_BASIC_AUTH` / `ALERTMANAGER_BASIC_AUTH` wrong. Same `drift` cred used for the `/drift/` route. |
| `reload failed: HTTP 400` after a write | The YAML is valid but the *content* is invalid (bad PromQL, missing label, malformed matcher). The file is on disk; the live config is unchanged. Re-propose with a fix. |
| `secret file(s) not present yet` warning | Drop the file on the host before applying; otherwise AM will fail at notify time. |
| Agent edits `starter.yml` | It shouldn't — guard is enforced in `_managed_path()`. File a bug; the system prompt should also be reinforced. |
| `Permission denied` writing rules | uid mismatch. Container runs as 999; host paths must be `chown 999:999`. |

### Direct API smoke tests

These bypass Drift and let you confirm the underlying services work:

```bash
# vmalert (via Caddy)
curl -u drift:PASSWORD https://drift.example.com/vmalert/api/v1/rules | jq '.data.groups[].name'

# vmalert (localhost — no auth)
curl http://localhost:8880/vmalert/api/v1/alerts | jq

# Alertmanager
curl http://localhost:9093/am/api/v2/receivers
curl http://localhost:9093/am/api/v2/silences | jq 'length'
amtool --alertmanager.url=http://localhost:9093/am check-config /etc/alertmanager/alertmanager.yml
```

### Where to look in the code

| Concern | File |
|---|---|
| Tool handlers + schemas | `drift-agent/app/tools/alerts.py` |
| Tool registration | `drift-agent/app/agent.py` — `all_tools()`, `all_handlers()`, `_summarize_for_event()` |
| Agent guidance for propose/apply | `drift-agent/app/agent.py` — `SYSTEM_PROMPT` step 4 |
| Bind-mounts | `docker-compose.yml` — `drift-agent.volumes` |
| Env vars | `.env.example` and `config.py` |
| Rule storage | `/root/setup/victoria/alerts/` |
| AM config + secrets | `/root/setup/victoria/alertmanager.yml`, `/root/setup/victoria/am-secrets/` |

---

## Extension ideas

- **Nested routing**: support adding routes under existing labelled subtrees (e.g. `routes[severity=critical].routes`) rather than only top-level. Identity tracking gets harder; consider a `route_id` annotation in the YAML.
- **Templated annotations**: a tool that, given a rule name, attaches a richer `description` with Grafana panel deeplinks computed from the labels.
- **Alert history**: a tool that queries the `ALERTS_FOR_STATE` series vmalert writes back to VM, so the agent can answer "how often has this fired in the last week?"
- **PagerDuty / Slack receivers**: add type-specific propose tools that take `_file` references for their respective secret fields (`service_key_file`, `api_url_file`).
- **Tests-as-rules**: when the agent proposes a rule, it could also call `instant_query` with the same expr to check that the query returns the expected number of series before committing.
