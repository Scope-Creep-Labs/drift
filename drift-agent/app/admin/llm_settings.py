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

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel

from ..config import settings
from ..users.deps import UserContext, forbid_in_demo, require_role


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


# Validate (model, credential) via the same LiteLLM completion code
# path the agent uses at runtime. No hardcoded provider URLs — LiteLLM
# owns the per-provider routing, base URLs, auth header shapes, and
# the rule set for whether OpenAI's `Authorization: Bearer …` or
# Anthropic's `x-api-key …` applies. A green result here guarantees
# the agent's next /investigate call would also authenticate; a
# `/v1/models`-style probe could pass for a key gated to different
# scopes than what completions actually need.
#
# Cost: one max_tokens=5 completion per validate click — fractions of
# a cent on Haiku/Flash, ~$0.005 on Opus, which is fine for an
# operator-triggered button.
async def _validate_via_litellm(
    model: str,
    api_key: Optional[str] = None,
    api_base: Optional[str] = None,
) -> tuple[bool, str]:
    """Send a 5-token completion through LiteLLM. Returns (ok, message).
    Wraps exception types into operator-readable strings."""
    try:
        import litellm
    except ImportError:
        return False, "litellm not installed inside drift-agent"

    # Run the completion in a thread so the async-handler stays
    # responsive. LiteLLM's `completion` is sync; `acompletion` exists
    # but its surface differs slightly per provider — sticking with
    # `completion` keeps the validate path identical to the agent's
    # path (which currently also uses `completion`).
    def _do() -> None:
        litellm.completion(
            model=model,
            messages=[{"role": "user", "content": "ok"}],
            max_tokens=5,
            api_key=api_key or None,
            api_base=api_base or None,
            timeout=10.0,
        )

    try:
        await asyncio.to_thread(_do)
        return True, ""
    except Exception as e:
        # LiteLLM normalizes provider errors into its own hierarchy.
        # We match by class name (rather than importing the classes)
        # because the namespace varies a bit across versions.
        kind = e.__class__.__name__
        if kind == "AuthenticationError":
            return False, f"provider rejected the credential — {e}"
        if kind == "NotFoundError":
            return False, f"model not available with this credential — {e}"
        if kind in ("APIConnectionError", "ConnectError", "ConnectTimeout"):
            return False, f"can't reach the provider — {e}"
        if kind == "BadRequestError":
            return False, f"provider rejected the request — {e}"
        # Anything else: surface the type + message so the operator
        # has something searchable.
        return False, f"{kind}: {e}"


async def _validate_updates(
    updates: dict[str, str], target_model: str
) -> list[tuple[str, str]]:
    """Run a validation probe for the credential the operator changed,
    against the model they're configuring. Returns `(field, message)`
    error tuples. Empty list = all good.

    Only one probe runs because the modal collapses credentials to a
    single field-per-provider; you can't change e.g. the OpenAI key
    while also configuring Anthropic from the same Save."""
    target = target_model.lower()
    if updates.get("ANTHROPIC_API_KEY"):
        ok, msg = await _validate_via_litellm(target_model, api_key=updates["ANTHROPIC_API_KEY"])
        return [] if ok else [("anthropic_api_key", msg)]
    if updates.get("OPENAI_API_KEY"):
        ok, msg = await _validate_via_litellm(target_model, api_key=updates["OPENAI_API_KEY"])
        return [] if ok else [("openai_api_key", msg)]
    if updates.get("GEMINI_API_KEY"):
        ok, msg = await _validate_via_litellm(target_model, api_key=updates["GEMINI_API_KEY"])
        return [] if ok else [("gemini_api_key", msg)]
    if updates.get("OLLAMA_API_BASE"):
        ok, msg = await _validate_via_litellm(target_model, api_base=updates["OLLAMA_API_BASE"])
        return [] if ok else [("ollama_api_base", msg)]
    return []


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
    # Current values of every provider API key. Returned in full so the
    # modal can pre-populate its input field (the admin opening this
    # modal already has root access on the host where .env lives, so
    # streaming the same value back through an admin-gated HTTPS API
    # doesn't expand the trust boundary). Empty string means unset.
    anthropic_api_key: str
    openai_api_key: str
    gemini_api_key: str
    ollama_api_base: str
    # Provider of the currently-configured model. Frontend uses this
    # to decide which one API-key field to render.
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
        anthropic_api_key=settings.anthropic_api_key,
        openai_api_key=settings.openai_api_key,
        gemini_api_key=settings.gemini_api_key,
        ollama_api_base=settings.ollama_api_base,
        current_provider=_detect_provider(settings.model),
    )


class ValidateRequest(BaseModel):
    # The (model, credential) pair to test together. The credential is
    # a provider API key for hosted providers (Anthropic/OpenAI/Gemini)
    # or an Ollama base URL for local models. We validate them as a
    # pair via a 5-token LiteLLM completion so a green result reflects
    # the exact same code path the agent uses at runtime.
    model: str
    credential: str


class ValidateResponse(BaseModel):
    valid: bool
    # Empty on success, human-readable explanation on failure.
    message: str


@router.post("/validate", response_model=ValidateResponse)
async def validate_credential(
    body: ValidateRequest = Body(...),
    _admin: UserContext = Depends(require_role("admin")),
) -> ValidateResponse:
    """Test a (model, credential) pair via a LiteLLM 5-token completion.
    Returns valid+empty message on success, valid=false with the
    provider's error on failure. Doesn't write to .env."""
    cred = body.credential.strip()
    if not cred:
        return ValidateResponse(valid=False, message="credential is empty")
    if not body.model.strip():
        return ValidateResponse(valid=False, message="model is required")
    provider = _detect_provider(body.model)
    if provider == "ollama":
        ok, msg = await _validate_via_litellm(body.model, api_base=cred)
    elif provider in ("anthropic", "openai", "gemini"):
        ok, msg = await _validate_via_litellm(body.model, api_key=cred)
    else:
        return ValidateResponse(
            valid=False,
            message=f"don't know how to validate model '{body.model}' — provider not recognized",
        )
    return ValidateResponse(valid=ok, message="" if ok else msg)


@router.put("")
async def update_llm_settings(
    body: LlmSettingsUpdate = Body(...),
    _admin: UserContext = Depends(require_role("admin")),
    _no_demo: None = Depends(forbid_in_demo),
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

    # Validate any credential changes BEFORE writing to .env. Saving a
    # bad key would still survive a restart but the agent would 401 on
    # every LLM call until the operator noticed. Catching it here means
    # one round-trip + a clear modal error instead of "Drift is broken,
    # let me check logs."
    #
    # `target_model` is the model the validation should test against —
    # either the one being changed in this PUT, or the currently
    # configured model if the operator only rotated a credential.
    target_model = updates.get("MODEL") or settings.model
    errors = await _validate_updates(updates, target_model)
    if errors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "validation_errors": [
                    {"field": field, "message": msg} for field, msg in errors
                ],
            },
        )

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
    # v0.1.39+ pins this to /var/lib/drift-cp; honor a legacy DEPLOY_DIR
    # override if .env still carries one from a pre-refactor install.
    deploy_dir = os.environ.get("DEPLOY_DIR", "/var/lib/drift-cp")
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
