from __future__ import annotations

import base64
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

from ..config import settings
from .metrics import ToolContext


MANAGED_FILE = "drift-managed.yml"  # only file the agent is allowed to write
MANAGED_GROUP = "drift-managed"      # single group inside that file


# Single-component (`30m`) AND combined (`1h30m`, `2d6h`, `45s`) forms.
# Models reach for whichever shape is natural to the request — `silence
# X for 90 minutes` becomes `90m` from one model, `1h30m` from another,
# `5400` from a third. Accept all three.
_DURATION_COMPONENT = re.compile(r"(\d+)\s*([smhdw])")
_DURATION_FULL = re.compile(r"^(\d+\s*[smhdw]\s*)+$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _duration_seconds(s: str) -> int:
    """Parse a duration string into seconds.

    Accepted shapes:
      - Single unit:  '45s', '30m', '1h', '7d', '2w'
      - Combined:     '1h30m', '2d12h', '1w3d', '90m30s'
      - Bare integer: '60'  (interpreted as seconds — what models reach
        for when they're not sure of the unit suffix)
      - Stripped of incidental whitespace before parsing.
    """
    s = s.strip()
    # Bare integer = seconds. Common when the model decides "I'll just
    # send the number" without committing to a unit.
    if s.isdigit():
        return int(s)
    if not _DURATION_FULL.match(s):
        raise ValueError(
            f"invalid duration: {s!r}. Accepted forms: '45s', '30m', '1h', "
            "'7d', '2w', combined like '1h30m' / '2d12h', or a bare integer "
            "(interpreted as seconds)."
        )
    return sum(
        int(n) * _UNIT_SECONDS[unit]
        for n, unit in _DURATION_COMPONENT.findall(s)
    )


# ---------- HTTP client ----------


class AlertClient:
    """Thin wrapper over vmalert + Alertmanager HTTP APIs.

    Both endpoints are optional. Calling a method when the corresponding URL
    is unset returns a structured error so tool handlers can surface it
    without exception noise in the trace.
    """

    def __init__(
        self,
        vmalert_url: str,
        vmalert_basic_auth: str,
        alertmanager_url: str,
        alertmanager_basic_auth: str,
    ):
        self.vmalert = vmalert_url.rstrip("/")
        self.alertmanager = alertmanager_url.rstrip("/")
        self._vmalert = httpx.AsyncClient(
            timeout=20.0, headers=_basic_auth_headers(vmalert_basic_auth)
        )
        self._am = httpx.AsyncClient(
            timeout=20.0, headers=_basic_auth_headers(alertmanager_basic_auth)
        )

    async def aclose(self) -> None:
        await self._vmalert.aclose()
        await self._am.aclose()

    # --- vmalert ---

    async def vmalert_get(self, path: str) -> dict:
        if not self.vmalert:
            return {"error": "VMALERT_URL not configured"}
        r = await self._vmalert.get(f"{self.vmalert}{path}")
        r.raise_for_status()
        return r.json()

    # --- alertmanager ---

    async def am_get(self, path: str) -> Any:
        if not self.alertmanager:
            return {"error": "ALERTMANAGER_URL not configured"}
        r = await self._am.get(f"{self.alertmanager}{path}")
        r.raise_for_status()
        return r.json()

    async def am_post(self, path: str, body: dict) -> Any:
        if not self.alertmanager:
            return {"error": "ALERTMANAGER_URL not configured"}
        r = await self._am.post(f"{self.alertmanager}{path}", json=body)
        r.raise_for_status()
        return r.json() if r.content else {}

    async def am_delete(self, path: str) -> Any:
        if not self.alertmanager:
            return {"error": "ALERTMANAGER_URL not configured"}
        r = await self._am.delete(f"{self.alertmanager}{path}")
        r.raise_for_status()
        return {}

    async def vmalert_reload(self) -> dict:
        """Trigger vmalert hot-reload after a rule-file change."""
        if not self.vmalert:
            return {"error": "VMALERT_URL not configured"}
        r = await self._vmalert.post(f"{self.vmalert}/-/reload")
        if r.status_code >= 400:
            return {"error": f"reload failed: HTTP {r.status_code} {r.text[:200]}"}
        return {"reloaded": True}

    async def am_reload(self) -> dict:
        """Trigger Alertmanager hot-reload after a config change."""
        if not self.alertmanager:
            return {"error": "ALERTMANAGER_URL not configured"}
        r = await self._am.post(f"{self.alertmanager}/-/reload")
        if r.status_code >= 400:
            return {"error": f"reload failed: HTTP {r.status_code} {r.text[:200]}"}
        return {"reloaded": True}


def _basic_auth_headers(auth: str) -> dict[str, str]:
    if not auth:
        return {}
    return {"Authorization": "Basic " + base64.b64encode(auth.encode()).decode()}


def make_alert_client() -> AlertClient:
    return AlertClient(
        settings.vmalert_url,
        settings.vmalert_basic_auth,
        settings.alertmanager_url,
        settings.alertmanager_basic_auth,
    )


# ---------- Tool implementations ----------


async def list_alert_rules(ctx: ToolContext, args: dict) -> dict:
    """Return all configured vmalert rules, optionally filtered by name substring."""
    raw = await ctx.alerts.vmalert_get("/api/v1/rules")
    if isinstance(raw, dict) and "error" in raw:
        return raw
    needle = (args.get("contains") or "").lower()
    groups_out = []
    total = 0
    for g in raw.get("data", {}).get("groups", []):
        rules = []
        for r in g.get("rules", []):
            if r.get("type") != "alerting":
                continue
            if needle and needle not in r.get("name", "").lower():
                continue
            rules.append(
                {
                    "name": r.get("name"),
                    "state": r.get("state"),
                    "expr": r.get("query"),
                    "for": r.get("duration"),
                    "labels": r.get("labels") or {},
                    "annotations": {
                        k: v for k, v in (r.get("annotations") or {}).items()
                        if k in ("summary", "description")
                    },
                    "last_error": r.get("lastError") or None,
                }
            )
            total += 1
        if rules:
            groups_out.append({"name": g.get("name"), "file": g.get("file"), "rules": rules})
    return {"n": total, "groups": groups_out}


async def list_active_alerts(ctx: ToolContext, args: dict) -> dict:
    """Return currently firing or pending alerts from vmalert."""
    raw = await ctx.alerts.vmalert_get("/api/v1/alerts")
    if isinstance(raw, dict) and "error" in raw:
        return raw
    state_filter = (args.get("state") or "").lower()
    out = []
    for a in raw.get("data", {}).get("alerts", []):
        st = (a.get("state") or "").lower()
        if state_filter and st != state_filter:
            continue
        out.append(
            {
                "name": a.get("name"),
                "state": st,
                "labels": a.get("labels") or {},
                "annotations": a.get("annotations") or {},
                "active_at": a.get("activeAt"),
                "value": a.get("value"),
            }
        )
    return {"n": len(out), "alerts": out[:50], "truncated_to": min(50, len(out))}


async def list_silences(ctx: ToolContext, args: dict) -> dict:
    """Return Alertmanager silences. By default only active ones; pass include_expired=true to see history."""
    raw = await ctx.alerts.am_get("/api/v2/silences")
    if isinstance(raw, dict) and "error" in raw:
        return raw
    include_expired = bool(args.get("include_expired"))
    out = []
    for s in raw or []:
        st = (s.get("status") or {}).get("state")
        if not include_expired and st != "active":
            continue
        out.append(
            {
                "id": s.get("id"),
                "state": st,
                "matchers": s.get("matchers"),
                "starts_at": s.get("startsAt"),
                "ends_at": s.get("endsAt"),
                "created_by": s.get("createdBy"),
                "comment": s.get("comment"),
            }
        )
    return {"n": len(out), "silences": out}


async def list_receivers(ctx: ToolContext, _args: dict) -> dict:
    """Return Alertmanager receivers (notification destinations)."""
    raw = await ctx.alerts.am_get("/api/v2/receivers")
    if isinstance(raw, dict) and "error" in raw:
        return raw
    names = [r.get("name") for r in (raw or []) if r.get("name")]
    return {"n": len(names), "receivers": names}


async def silence_alert(ctx: ToolContext, args: dict) -> dict:
    """Create an Alertmanager silence. `matchers` is a list of {name, value, isRegex, isEqual?}."""
    matchers = args.get("matchers") or []
    if not matchers:
        return {"error": "at least one matcher is required"}
    duration = args.get("duration") or "1h"
    try:
        delta_seconds = _duration_seconds(duration)
    except ValueError as e:
        return {"error": str(e)}
    now = datetime.now(timezone.utc)
    body = {
        "matchers": [
            {
                "name": m["name"],
                "value": m["value"],
                "isRegex": bool(m.get("isRegex", False)),
                "isEqual": bool(m.get("isEqual", True)),
            }
            for m in matchers
        ],
        "startsAt": now.isoformat().replace("+00:00", "Z"),
        "endsAt": (now + timedelta(seconds=delta_seconds)).isoformat().replace("+00:00", "Z"),
        "createdBy": args.get("created_by") or "drift",
        "comment": args.get("comment") or "Silenced via Drift",
    }
    result = await ctx.alerts.am_post("/api/v2/silences", body)
    if isinstance(result, dict) and "error" in result:
        return result
    return {"silence_id": result.get("silenceID"), "ends_at": body["endsAt"]}


async def delete_silence(ctx: ToolContext, args: dict) -> dict:
    """Expire an Alertmanager silence by id."""
    sid = args.get("id")
    if not sid:
        return {"error": "id is required"}
    result = await ctx.alerts.am_delete(f"/api/v2/silence/{sid}")
    if isinstance(result, dict) and "error" in result:
        return result
    return {"deleted": sid}


# ---------- Rule file mutation ----------


def _managed_path() -> Path:
    return Path(settings.vmalert_rules_dir) / MANAGED_FILE


def _load_managed() -> dict:
    """Read drift-managed.yml. Returns the parsed structure or a fresh skeleton."""
    p = _managed_path()
    if not p.exists():
        return {"groups": [{"name": MANAGED_GROUP, "interval": "30s", "rules": []}]}
    with p.open("r") as f:
        data = yaml.safe_load(f) or {}
    if not data.get("groups"):
        data = {"groups": [{"name": MANAGED_GROUP, "interval": "30s", "rules": []}]}
    return data


def _save_managed(data: dict) -> None:
    """Atomic write: tmp file in the same dir + rename."""
    p = _managed_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # NamedTemporaryFile in the same dir so rename is atomic (same filesystem).
    with tempfile.NamedTemporaryFile(
        "w", dir=str(p.parent), prefix=".drift-managed.", suffix=".yml.tmp", delete=False
    ) as tmp:
        yaml.safe_dump(data, tmp, sort_keys=False, default_flow_style=False)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = tmp.name
    os.replace(tmp_path, str(p))


def _find_rule(data: dict, name: str) -> tuple[dict | None, int | None, int | None]:
    """Locate a rule by name. Returns (rule, group_idx, rule_idx) or (None, None, None)."""
    for gi, g in enumerate(data.get("groups", [])):
        for ri, r in enumerate(g.get("rules", []) or []):
            if r.get("alert") == name:
                return r, gi, ri
    return None, None, None


def _build_rule(args: dict) -> dict:
    """Construct a rule dict from tool input. Skips empty fields for tidy YAML."""
    rule: dict = {
        "alert": args["name"],
        "expr": args["expr"],
    }
    if args.get("for"):
        rule["for"] = args["for"]
    if args.get("labels"):
        rule["labels"] = args["labels"]
    if args.get("annotations"):
        rule["annotations"] = args["annotations"]
    return rule


async def propose_alert_rule(_ctx: ToolContext, args: dict) -> dict:
    """Preview the YAML write that `apply_alert_rule` would perform. No side effect."""
    try:
        rule = _build_rule(args)
    except KeyError as e:
        return {"error": f"missing required field: {e.args[0]}"}
    data = _load_managed()
    existing, _, _ = _find_rule(data, rule["alert"])
    action = "update" if existing else "create"
    preview_yaml = yaml.safe_dump({"groups": [{"name": MANAGED_GROUP, "rules": [rule]}]},
                                  sort_keys=False, default_flow_style=False)
    return {
        "action": action,
        "name": rule["alert"],
        "file": str(_managed_path()),
        "yaml": preview_yaml,
        "existing": existing,
    }


async def apply_alert_rule(ctx: ToolContext, args: dict) -> dict:
    """Upsert a rule in drift-managed.yml, then trigger a vmalert reload."""
    try:
        rule = _build_rule(args)
    except KeyError as e:
        return {"error": f"missing required field: {e.args[0]}"}

    data = _load_managed()
    # Ensure the managed group exists at index 0 (we write into it).
    if not data.get("groups") or data["groups"][0].get("name") != MANAGED_GROUP:
        data.setdefault("groups", []).insert(
            0, {"name": MANAGED_GROUP, "interval": "30s", "rules": []}
        )

    _, gi, ri = _find_rule(data, rule["alert"])
    if gi is not None and ri is not None:
        data["groups"][gi]["rules"][ri] = rule
        action = "updated"
    else:
        data["groups"][0]["rules"].append(rule)
        action = "created"

    try:
        _save_managed(data)
    except OSError as e:
        return {"error": f"write failed: {e}"}

    reload_result = await ctx.alerts.vmalert_reload()
    if isinstance(reload_result, dict) and "error" in reload_result:
        return {
            "warning": "rule written but vmalert reload failed",
            "action": action,
            "name": rule["alert"],
            "reload_error": reload_result["error"],
        }
    return {"action": action, "name": rule["alert"], "file": str(_managed_path())}


async def delete_alert_rule(ctx: ToolContext, args: dict) -> dict:
    """Remove a rule from drift-managed.yml + reload. Won't touch other files."""
    name = args.get("name")
    if not name:
        return {"error": "name is required"}

    p = _managed_path()
    if not p.exists():
        return {"error": f"{MANAGED_FILE} does not exist; no drift-managed rules to delete"}

    data = _load_managed()
    _, gi, ri = _find_rule(data, name)
    if gi is None:
        return {"error": f"rule '{name}' not found in {MANAGED_FILE} (hand-edited rules in other files aren't managed by Drift)"}

    removed = data["groups"][gi]["rules"].pop(ri)
    try:
        _save_managed(data)
    except OSError as e:
        return {"error": f"write failed: {e}"}

    reload_result = await ctx.alerts.vmalert_reload()
    if isinstance(reload_result, dict) and "error" in reload_result:
        return {
            "warning": "rule removed but vmalert reload failed",
            "deleted": name,
            "reload_error": reload_result["error"],
        }
    return {"deleted": name, "was": removed}


# ---------- Alertmanager config (receivers + routes) ----------


def _am_config_path() -> Path:
    return Path(settings.alertmanager_config_file)


def _am_secrets_dir() -> Path:
    return Path(settings.alertmanager_secrets_dir)


def _load_am_config() -> dict:
    p = _am_config_path()
    if not p.exists():
        return {"route": {"receiver": "default"}, "receivers": [{"name": "default"}]}
    with p.open("r") as f:
        return yaml.safe_load(f) or {}


def _save_am_config(data: dict) -> None:
    p = _am_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=str(p.parent), prefix=".alertmanager.", suffix=".yml.tmp", delete=False
    ) as tmp:
        yaml.safe_dump(data, tmp, sort_keys=False, default_flow_style=False)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = tmp.name
    os.replace(tmp_path, str(p))


def _secret_path(filename: str) -> str:
    """Validate a secrets filename (basename only) and return the full mount path."""
    if not filename or "/" in filename or filename.startswith(".") or len(filename) > 100:
        raise ValueError(
            "secret filename must be a non-empty basename without '/' (e.g. 'ntfy-default')"
        )
    return str(_am_secrets_dir() / filename)


def _build_webhook_receiver(args: dict) -> tuple[dict, list[str]]:
    """Construct a webhook receiver block. Returns (receiver_dict, missing_secret_files)."""
    name = args["name"]
    url = args.get("url")
    url_file = args.get("url_file")
    if not url and not url_file:
        raise ValueError("either `url` or `url_file` is required")
    if url and url_file:
        raise ValueError("provide `url` OR `url_file`, not both")

    cfg: dict = {}
    if url:
        cfg["url"] = url
    else:
        cfg["url_file"] = _secret_path(url_file)
    cfg["send_resolved"] = bool(args.get("send_resolved", True))

    missing: list[str] = []
    if url_file and not Path(cfg["url_file"]).exists():
        missing.append(cfg["url_file"])

    auth = (args.get("auth") or "none").lower()
    if auth not in ("none", "bearer", "basic"):
        raise ValueError("auth must be one of: none, bearer, basic")

    http_cfg: dict = {}
    if auth == "bearer":
        cred_file = args.get("auth_credentials_file")
        if not cred_file:
            raise ValueError("auth_credentials_file is required for bearer auth")
        cred_path = _secret_path(cred_file)
        http_cfg["authorization"] = {"type": "Bearer", "credentials_file": cred_path}
        if not Path(cred_path).exists():
            missing.append(cred_path)
    elif auth == "basic":
        username = args.get("auth_basic_username")
        cred_file = args.get("auth_credentials_file")
        if not username or not cred_file:
            raise ValueError("auth_basic_username and auth_credentials_file are required for basic auth")
        cred_path = _secret_path(cred_file)
        http_cfg["basic_auth"] = {"username": username, "password_file": cred_path}
        if not Path(cred_path).exists():
            missing.append(cred_path)
    if http_cfg:
        cfg["http_config"] = http_cfg

    return {"name": name, "webhook_configs": [cfg]}, missing


def _find_receiver_idx(data: dict, name: str) -> int | None:
    for i, r in enumerate(data.get("receivers", []) or []):
        if r.get("name") == name:
            return i
    return None


def _find_top_route_idx(data: dict, receiver_name: str) -> int | None:
    """Index into route.routes for the top-level route targeting receiver_name."""
    routes = (data.get("route") or {}).get("routes") or []
    for i, r in enumerate(routes):
        if r.get("receiver") == receiver_name:
            return i
    return None


async def propose_receiver(_ctx: ToolContext, args: dict) -> dict:
    """Preview a webhook receiver write. No side effect. Warns about missing secrets."""
    try:
        receiver, missing = _build_webhook_receiver(args)
    except (KeyError, ValueError) as e:
        return {"error": str(e)}
    data = _load_am_config()
    existing_idx = _find_receiver_idx(data, receiver["name"])
    action = "update" if existing_idx is not None else "create"
    preview = yaml.safe_dump({"receivers": [receiver]}, sort_keys=False, default_flow_style=False)
    out = {
        "action": action,
        "name": receiver["name"],
        "yaml": preview,
        "config_file": str(_am_config_path()),
    }
    if missing:
        out["warning"] = (
            "secret file(s) not present yet: "
            + ", ".join(missing)
            + ". Create them on the host before applying, or AM will fail at notify time."
        )
    return out


async def upsert_receiver(ctx: ToolContext, args: dict) -> dict:
    """Add or replace a webhook receiver in alertmanager.yml, then reload AM."""
    try:
        receiver, missing = _build_webhook_receiver(args)
    except (KeyError, ValueError) as e:
        return {"error": str(e)}

    data = _load_am_config()
    data.setdefault("receivers", [])
    idx = _find_receiver_idx(data, receiver["name"])
    if idx is not None:
        data["receivers"][idx] = receiver
        action = "updated"
    else:
        data["receivers"].append(receiver)
        action = "created"

    try:
        _save_am_config(data)
    except OSError as e:
        return {"error": f"write failed: {e}"}

    reload_result = await ctx.alerts.am_reload()
    if isinstance(reload_result, dict) and "error" in reload_result:
        return {
            "warning": "receiver written but Alertmanager reload failed",
            "action": action,
            "name": receiver["name"],
            "reload_error": reload_result["error"],
            "missing_secrets": missing,
        }
    return {
        "action": action,
        "name": receiver["name"],
        "missing_secrets": missing,
    }


async def delete_receiver(ctx: ToolContext, args: dict) -> dict:
    """Remove a receiver from alertmanager.yml + reload. Refuses if a route still references it."""
    name = args.get("name")
    if not name:
        return {"error": "name is required"}
    if name == "default":
        return {"error": "cannot remove the default receiver"}

    data = _load_am_config()
    idx = _find_receiver_idx(data, name)
    if idx is None:
        return {"error": f"receiver '{name}' not found"}

    # Check no top-level route references it.
    if _find_top_route_idx(data, name) is not None:
        return {
            "error": f"receiver '{name}' is still referenced by a route — "
                     f"call delete_route first or both at once."
        }

    removed = data["receivers"].pop(idx)
    try:
        _save_am_config(data)
    except OSError as e:
        return {"error": f"write failed: {e}"}

    reload_result = await ctx.alerts.am_reload()
    if isinstance(reload_result, dict) and "error" in reload_result:
        return {
            "warning": "receiver removed but Alertmanager reload failed",
            "deleted": name,
            "reload_error": reload_result["error"],
        }
    return {"deleted": name, "was": removed}


async def set_route(ctx: ToolContext, args: dict) -> dict:
    """Upsert a top-level matcher-based route in alertmanager.yml + reload.

    Identity is the target receiver name. One route per receiver in the
    top-level `route.routes` array (nested-route trees stay hand-managed).
    """
    receiver = args.get("receiver")
    matchers = args.get("matchers") or []
    if not receiver:
        return {"error": "receiver is required"}
    if not matchers:
        return {"error": "at least one matcher is required (e.g. [{name: 'severity', value: 'critical'}])"}

    # Build the matcher strings AM expects (e.g. 'severity="critical"', 'host=~"pi.*"').
    matcher_strs: list[str] = []
    for m in matchers:
        n = m.get("name")
        v = m.get("value")
        if not n or v is None:
            return {"error": "each matcher needs name + value"}
        is_regex = bool(m.get("isRegex", False))
        is_equal = bool(m.get("isEqual", True))
        op = ("=~" if is_regex else "=") if is_equal else ("!~" if is_regex else "!=")
        matcher_strs.append(f'{n}{op}"{v}"')

    route_block: dict = {"receiver": receiver, "matchers": matcher_strs}
    if args.get("continue") is not None:
        route_block["continue"] = bool(args["continue"])
    if args.get("group_wait"):
        route_block["group_wait"] = args["group_wait"]
    if args.get("group_interval"):
        route_block["group_interval"] = args["group_interval"]
    if args.get("repeat_interval"):
        route_block["repeat_interval"] = args["repeat_interval"]

    data = _load_am_config()
    if _find_receiver_idx(data, receiver) is None:
        return {"error": f"receiver '{receiver}' does not exist — create it with upsert_receiver first"}
    root_route = data.setdefault("route", {})
    routes = root_route.setdefault("routes", [])
    idx = _find_top_route_idx(data, receiver)
    if idx is not None:
        routes[idx] = route_block
        action = "updated"
    else:
        routes.append(route_block)
        action = "created"

    try:
        _save_am_config(data)
    except OSError as e:
        return {"error": f"write failed: {e}"}

    reload_result = await ctx.alerts.am_reload()
    if isinstance(reload_result, dict) and "error" in reload_result:
        return {
            "warning": "route written but Alertmanager reload failed",
            "action": action,
            "receiver": receiver,
            "reload_error": reload_result["error"],
        }
    return {"action": action, "receiver": receiver, "matchers": matcher_strs}


async def delete_route(ctx: ToolContext, args: dict) -> dict:
    """Remove the top-level route for a given receiver."""
    receiver = args.get("receiver")
    if not receiver:
        return {"error": "receiver is required"}

    data = _load_am_config()
    idx = _find_top_route_idx(data, receiver)
    if idx is None:
        return {"error": f"no top-level route targeting receiver '{receiver}' found"}

    removed = data["route"]["routes"].pop(idx)
    try:
        _save_am_config(data)
    except OSError as e:
        return {"error": f"write failed: {e}"}

    reload_result = await ctx.alerts.am_reload()
    if isinstance(reload_result, dict) and "error" in reload_result:
        return {
            "warning": "route removed but Alertmanager reload failed",
            "deleted_for": receiver,
            "reload_error": reload_result["error"],
        }
    return {"deleted_for": receiver, "was": removed}


# ---------- Schemas ----------


ALERT_TOOLS: list[dict] = [
    {
        "name": "list_alert_rules",
        "description": (
            "List configured vmalert alerting rules. Each entry includes the name, "
            "PromQL expression, `for` duration, current state (inactive/pending/firing), "
            "labels, and annotations. Use this when answering 'what alerts do we have?' "
            "or before proposing a new one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contains": {
                    "type": "string",
                    "description": "Case-insensitive substring filter on rule name.",
                },
            },
        },
    },
    {
        "name": "list_active_alerts",
        "description": (
            "List alerts currently firing or pending in vmalert. Use this to answer "
            "'what's wrong right now?' or to identify alerts a user wants to silence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "enum": ["firing", "pending"],
                    "description": "Restrict to one state. Default: all states.",
                },
            },
        },
    },
    {
        "name": "list_silences",
        "description": (
            "List Alertmanager silences. By default returns only active silences; "
            "set include_expired=true to also see expired ones (history)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "include_expired": {
                    "type": "boolean",
                    "description": "Include expired silences in the result.",
                },
            },
        },
    },
    {
        "name": "list_receivers",
        "description": "List Alertmanager receivers (notification destinations like Slack, ntfy, webhook).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "silence_alert",
        "description": (
            "Create an Alertmanager silence. Provide one or more label matchers; metrics "
            "whose alerts match all of them are suppressed for the given duration. "
            "Confirm with the user before silencing if the matcher is broad (e.g. only "
            "`severity=warning`) — accidentally silencing too much hides real problems."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "matchers": {
                    "type": "array",
                    "description": "Label matchers to suppress.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Label name."},
                            "value": {"type": "string", "description": "Label value (or regex)."},
                            "isRegex": {"type": "boolean", "description": "Treat value as regex."},
                            "isEqual": {
                                "type": "boolean",
                                "description": "True = match (default), false = NOT match.",
                            },
                        },
                        "required": ["name", "value"],
                    },
                },
                "duration": {
                    "type": "string",
                    "description": (
                        "How long to silence. Single unit ('30m', '1h', '7d', '2w'), "
                        "combined ('1h30m', '2d12h'), or a bare integer (seconds). "
                        "Default 1h."
                    ),
                },
                "comment": {"type": "string", "description": "Human-readable reason."},
                "created_by": {"type": "string", "description": "Attribution. Default 'drift'."},
            },
            "required": ["matchers"],
        },
    },
    {
        "name": "delete_silence",
        "description": "Expire an Alertmanager silence by its id (uuid).",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "Silence id (uuid)."}},
            "required": ["id"],
        },
    },
    {
        "name": "propose_alert_rule",
        "description": (
            "Preview a rule write without applying it. Use this BEFORE `apply_alert_rule` "
            "whenever the user asks to create or change a rule, and surface the proposed "
            "YAML to them so they can confirm. Returns whether the action would be a "
            "create or update."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Alert name (PascalCase by convention)."},
                "expr": {"type": "string", "description": "PromQL/MetricsQL expression that, when truthy, fires the alert."},
                "for": {"type": "string", "description": "Duration the expr must hold before firing (e.g. '5m', '10m', '1h'). Optional."},
                "labels": {
                    "type": "object",
                    "description": "Static labels attached to firing alerts (e.g. severity, team).",
                    "additionalProperties": {"type": "string"},
                },
                "annotations": {
                    "type": "object",
                    "description": "Templated strings shown in notifications. Use $labels.<name> for label interpolation.",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["name", "expr"],
        },
    },
    {
        "name": "apply_alert_rule",
        "description": (
            "Upsert a rule in the agent-managed rule file (drift-managed.yml) and trigger "
            "vmalert to hot-reload. Use AFTER `propose_alert_rule` and the user confirms. "
            "Identity is the alert name: same name = update, new name = append."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "expr": {"type": "string"},
                "for": {"type": "string"},
                "labels": {"type": "object", "additionalProperties": {"type": "string"}},
                "annotations": {"type": "object", "additionalProperties": {"type": "string"}},
            },
            "required": ["name", "expr"],
        },
    },
    {
        "name": "delete_alert_rule",
        "description": (
            "Remove a rule from the agent-managed rule file and reload vmalert. Refuses "
            "to delete rules in hand-edited files (e.g. starter.yml) — those must be "
            "removed manually."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Alert name to remove."}},
            "required": ["name"],
        },
    },
    {
        "name": "propose_receiver",
        "description": (
            "Preview a webhook-receiver write to alertmanager.yml. No side effect. Covers "
            "ntfy (via bearer auth) and generic webhooks. Secrets (bearer tokens, basic-auth "
            "passwords, even the URL when it's sensitive) are referenced by FILENAME — the "
            "agent never sees the secret contents. Returns a warning if the referenced "
            "secret file isn't on disk yet."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Receiver name (kebab-case by convention)."},
                "url": {"type": "string", "description": "Webhook URL when it's not sensitive (e.g. an ntfy topic URL)."},
                "url_file": {
                    "type": "string",
                    "description": "Filename (basename only) inside the secrets dir when the URL itself is sensitive (e.g. discord-style hash URLs).",
                },
                "send_resolved": {"type": "boolean", "description": "Notify on resolution. Default true."},
                "auth": {
                    "type": "string",
                    "enum": ["none", "bearer", "basic"],
                    "description": "Auth scheme. Default 'none'.",
                },
                "auth_credentials_file": {
                    "type": "string",
                    "description": "Filename inside secrets dir holding the bearer token or basic-auth password.",
                },
                "auth_basic_username": {
                    "type": "string",
                    "description": "Username (required when auth='basic').",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "upsert_receiver",
        "description": (
            "Add or replace a webhook receiver in alertmanager.yml, then reload AM. Use "
            "AFTER propose_receiver and user confirmation. Idempotent by receiver name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "url": {"type": "string"},
                "url_file": {"type": "string"},
                "send_resolved": {"type": "boolean"},
                "auth": {"type": "string", "enum": ["none", "bearer", "basic"]},
                "auth_credentials_file": {"type": "string"},
                "auth_basic_username": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "delete_receiver",
        "description": (
            "Remove a receiver from alertmanager.yml + reload. Refuses if a top-level "
            "route still references it (delete the route first)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "set_route",
        "description": (
            "Upsert a top-level matcher-based route in alertmanager.yml. Binds alerts "
            "whose labels match ALL matchers to the given receiver. Identity is the "
            "receiver name — one top-level route per receiver. Reloads AM after writing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "receiver": {"type": "string", "description": "Existing receiver name."},
                "matchers": {
                    "type": "array",
                    "description": "Label matchers (all must match).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "value": {"type": "string"},
                            "isRegex": {"type": "boolean"},
                            "isEqual": {"type": "boolean", "description": "True = match, false = NOT match."},
                        },
                        "required": ["name", "value"],
                    },
                },
                "continue": {"type": "boolean", "description": "Let later routes also match. Default false."},
                "group_wait": {"type": "string", "description": "Override default group_wait (e.g. '30s')."},
                "group_interval": {"type": "string"},
                "repeat_interval": {"type": "string"},
            },
            "required": ["receiver", "matchers"],
        },
    },
    {
        "name": "delete_route",
        "description": "Remove the top-level route for a given receiver from alertmanager.yml + reload.",
        "input_schema": {
            "type": "object",
            "properties": {"receiver": {"type": "string"}},
            "required": ["receiver"],
        },
    },
]


ALERT_HANDLERS = {
    "list_alert_rules": list_alert_rules,
    "list_active_alerts": list_active_alerts,
    "list_silences": list_silences,
    "list_receivers": list_receivers,
    "silence_alert": silence_alert,
    "delete_silence": delete_silence,
    "propose_alert_rule": propose_alert_rule,
    "apply_alert_rule": apply_alert_rule,
    "delete_alert_rule": delete_alert_rule,
    "propose_receiver": propose_receiver,
    "upsert_receiver": upsert_receiver,
    "delete_receiver": delete_receiver,
    "set_route": set_route,
    "delete_route": delete_route,
}
