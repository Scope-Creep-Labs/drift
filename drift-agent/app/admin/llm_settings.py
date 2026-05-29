"""Admin endpoints for changing the LLM model + API keys at runtime.

The CP's .env file is the source of truth for every setting the
installer wrote, including `MODEL`, `EFFORT`, `MAX_TOKENS`, and the
provider API keys. drift-agent's process environment is populated
from .env at container start via compose's `env_file:` directive, so
mutating .env on disk requires a container restart for the new
values to take effect.

This module exposes two endpoints:

  GET  /api/admin/llm-settings       — current state (model + key
                                       presence, never the keys
                                       themselves)
  PUT  /api/admin/llm-settings       — write to .env + schedule a
                                       drift-agent recreate via the
                                       same detached-helper pattern
                                       used by Software Updates

Security: admin role only. API keys are never returned in GET; PUT
accepts new keys verbatim and writes them through to .env. The
write is atomic (tempfile + rename in the same dir) and preserves
unrelated lines (comments, other vars) exactly as install.sh wrote
them.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel

from ..config import settings
from ..users.deps import UserContext, require_role


router = APIRouter(prefix="/api/admin/llm-settings", tags=["admin"])


# Path inside the container where the host's .env is bind-mounted (see
# deploy/docker-compose.yml). Settable via env for test setups, but the
# default matches what the installer ships and is what production runs.
CP_ENV_FILE = Path(os.environ.get("CP_ENV_FILE", "/etc/drift/cp.env"))


# Keys this endpoint touches. Everything else in .env is preserved
# verbatim. Setting a key to the empty string clears it (useful for
# rotating providers, e.g. swapping from Anthropic to Gemini).
MUTABLE_KEYS = (
    "MODEL",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "OLLAMA_API_BASE",
    "EFFORT",
    "MAX_TOKENS",
)

PROVIDER_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY")

_ENV_LINE = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")


def _read_env_file(path: Path) -> dict[str, str]:
    """Return a flat key→value dict of every assignment in `.env`. Lines
    that aren't assignments (comments, blanks) are skipped. Quotes are
    NOT stripped — we hand the value back the way it was written, and
    only callers that need a "clean" value un-quote at read time."""
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        m = _ENV_LINE.match(raw.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _write_env_file(path: Path, updates: dict[str, str]) -> None:
    """Update KEY=VALUE lines in `.env` in place.

    For each key in `updates`:
      - If the key already appears in the file, replace its value on
        the existing line (preserves position and any inline trailing
        whitespace).
      - If the key is new, append it at the end with a leading blank
        line if the file doesn't already end in one.

    Atomic via tempfile + os.replace; the temp file lives in the same
    directory so rename is a same-filesystem operation (POSIX atomic).
    Permissions of the existing file are preserved.
    """
    if not path.is_file():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"CP .env file not present at {path}; expected install.sh "
            "to have created it. Was drift-agent started outside the "
            "single-server bundle?",
        )

    original = path.read_text()
    lines = original.splitlines(keepends=True)
    seen: set[str] = set()
    out_lines: list[str] = []
    for line in lines:
        stripped = line.rstrip("\n").rstrip("\r")
        m = _ENV_LINE.match(stripped.strip())
        if m and m.group(1) in updates:
            key = m.group(1)
            seen.add(key)
            # Preserve trailing newline kind (CR/LF vs LF) the file
            # already used by keeping everything after the assignment.
            tail = line[len(stripped):]
            out_lines.append(f"{key}={updates[key]}{tail}")
        else:
            out_lines.append(line)
    # Append anything we didn't see in the existing file.
    appended: list[str] = []
    for key, val in updates.items():
        if key not in seen:
            appended.append(f"{key}={val}\n")
    if appended:
        if out_lines and not out_lines[-1].endswith("\n"):
            out_lines.append("\n")
        out_lines.append("\n")
        out_lines.extend(appended)

    new_content = "".join(out_lines)

    # Atomic write: same-dir tempfile + os.replace.
    tmp = path.with_suffix(path.suffix + ".tmp-llm")
    try:
        tmp.write_text(new_content)
        # Match the existing file's mode (typically 660 root:docker).
        st = path.stat()
        os.chmod(tmp, st.st_mode & 0o7777)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except PermissionError:
            # Best-effort; if we can't chown the tmp the rename still
            # succeeds and the file inherits our euid/egid. install.sh
            # re-chowns on next run.
            pass
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def _detect_provider(model: str) -> str:
    """Map a model id to the API-key environment variable that needs to
    be set for LiteLLM to authenticate it. Returns one of `anthropic`,
    `openai`, `gemini`, `ollama`, or `unknown`."""
    bare = model.split("/", 1)[-1] if "/" in model else model
    if model.startswith("ollama/") or model.startswith("ollama_chat/"):
        return "ollama"
    if bare.startswith("claude-") or model.startswith("anthropic/"):
        return "anthropic"
    if bare.startswith("gpt-") or bare.startswith("o1") or bare.startswith("o3"):
        return "openai"
    if bare.startswith("gemini-") or model.startswith("gemini/"):
        return "gemini"
    return "unknown"


class LlmSettingsOut(BaseModel):
    model: str
    effort: str
    max_tokens: int
    # Whether each provider's API key is non-empty. Values themselves
    # are NEVER returned; the modal only needs to know which keys are
    # already present so it can prompt only for missing ones.
    configured_keys: dict[str, bool]
    ollama_api_base: str
    # Provider of the currently-configured model. The frontend uses
    # this to decide which key field to highlight.
    current_provider: str


class LlmSettingsUpdate(BaseModel):
    model: Optional[str] = None
    effort: Optional[str] = None
    max_tokens: Optional[int] = None
    anthropic_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    ollama_api_base: Optional[str] = None


@router.get("", response_model=LlmSettingsOut)
async def get_llm_settings(
    _admin: UserContext = Depends(require_role("admin")),
) -> LlmSettingsOut:
    return LlmSettingsOut(
        model=settings.model,
        effort=settings.effort,
        max_tokens=settings.max_tokens,
        configured_keys={
            "anthropic": bool(settings.anthropic_api_key),
            "openai": bool(settings.openai_api_key),
            "gemini": bool(settings.gemini_api_key),
            "ollama": bool(settings.ollama_api_base),
        },
        ollama_api_base=settings.ollama_api_base,
        current_provider=_detect_provider(settings.model),
    )


@router.put("")
async def update_llm_settings(
    body: LlmSettingsUpdate = Body(...),
    _admin: UserContext = Depends(require_role("admin")),
) -> dict:
    # Build the {KEY: value} dict only with the fields the operator
    # actually changed. `None` means "leave alone"; empty string means
    # "clear this value" (e.g. rotating from Anthropic to Gemini, the
    # operator may want to wipe ANTHROPIC_API_KEY).
    updates: dict[str, str] = {}
    if body.model is not None:
        updates["MODEL"] = body.model.strip()
    if body.effort is not None:
        updates["EFFORT"] = body.effort.strip()
    if body.max_tokens is not None:
        updates["MAX_TOKENS"] = str(int(body.max_tokens))
    if body.anthropic_api_key is not None:
        updates["ANTHROPIC_API_KEY"] = body.anthropic_api_key.strip()
    if body.openai_api_key is not None:
        updates["OPENAI_API_KEY"] = body.openai_api_key.strip()
    if body.gemini_api_key is not None:
        updates["GEMINI_API_KEY"] = body.gemini_api_key.strip()
    if body.ollama_api_base is not None:
        updates["OLLAMA_API_BASE"] = body.ollama_api_base.strip()

    if not updates:
        return {"changed": False, "restart_scheduled": False}

    _write_env_file(CP_ENV_FILE, updates)

    # Schedule a drift-agent recreate via a detached helper. Same
    # pattern as the Software Updates apply: spawn a sibling container
    # that waits a few seconds (so this HTTP response can flush) and
    # then runs `docker compose up -d --no-deps drift-agent`, picking
    # up the freshly-written .env values on container start.
    restart_scheduled = _schedule_drift_agent_recreate()

    return {
        "changed": True,
        "restart_scheduled": restart_scheduled,
        "updated_keys": sorted(updates.keys() - set(PROVIDER_KEYS))
        + [k for k in updates if k in PROVIDER_KEYS],
    }


def _schedule_drift_agent_recreate() -> bool:
    """Spawn a detached helper container that runs `docker compose up
    -d --no-deps --force-recreate drift-agent` after a short delay.
    Returns True if the helper started, False if docker isn't
    reachable from inside this container.

    Why not call docker compose directly: it would block on the
    recreate while we're trying to return a response, and the recreate
    necessarily kills the running drift-agent — i.e. ourselves — which
    interrupts the HTTP flush. The helper-container indirection lets
    this endpoint respond cleanly first.
    """
    deploy_dir = os.environ.get("DEPLOY_DIR", "/root/setup/drift-deploy")
    helper_image = "ghcr.io/kidproquo/drift-agent:latest"
    cmd = [
        "docker", "run", "--rm", "-d",
        "--name", f"drift-llm-restart-helper-{int(asyncio.get_event_loop().time())}",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "-v", f"{deploy_dir}:{deploy_dir}",
        "--workdir", deploy_dir,
        helper_image,
        "sh", "-c",
        # Give the HTTP response time to flush, then recreate. --no-deps
        # so we don't bounce postgres/vm/etc.; --force-recreate guarantees
        # the new env is picked up even when image digest is unchanged.
        "sleep 3 && docker compose up -d --no-deps --force-recreate drift-agent",
    ]
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except (FileNotFoundError, OSError):
        return False
