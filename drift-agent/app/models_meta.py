"""Model metadata endpoint.

`GET /api/models/pricing` returns LiteLLM's pricing table for every
model LiteLLM knows about. Used by the frontend to compute token
costs in the sidebar usage display, replacing the previously
hardcoded `src/lib/pricing.ts` table.

Auth: any signed-in user. Pricing is public information (vendor
price lists) and showing it doesn't expose anything sensitive; gating
to admins only would block the observe/deploy roles from seeing their
own usage cost, which defeats the point.

Cache: pricing rarely changes within a process lifetime, so the
endpoint computes the response once and reuses it. A drift-agent
restart picks up whatever pricing the bundled LiteLLM version ships
with.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends

from .users.deps import UserContext, get_current_user


router = APIRouter(prefix="/api/models", tags=["models"])


_cached: Optional[dict] = None


def _build_pricing() -> dict:
    """Translate LiteLLM's `model_cost` dict (per-token USD) into a
    per-1M-token shape that's friendlier for display + downstream
    arithmetic. Returns a flat mapping `{model_id: {input, output,
    cache_read, cache_write}}` in USD/1M tokens.

    LiteLLM's keys are floats; missing channels are treated as 0.0.
    Non-chat entries (embeddings, image models) are filtered so the
    payload is compact and the frontend doesn't accidentally price an
    embedding model as a chat model.
    """
    try:
        import litellm
    except ImportError:  # pragma: no cover (litellm is a hard dep)
        return {}

    out: dict[str, dict[str, float]] = {}
    cost_table = getattr(litellm, "model_cost", {}) or {}
    for model_id, info in cost_table.items():
        if not isinstance(info, dict):
            continue
        # Skip non-chat entries to keep the payload small. LiteLLM tags
        # chat models as mode="chat"; embeddings, rerankers, etc. carry
        # other values. Missing mode → assume chat (safe default;
        # everything Drift cares about is chat-shaped).
        mode = info.get("mode", "chat")
        if mode not in ("chat", "completion"):
            continue
        in_per_token = float(info.get("input_cost_per_token") or 0)
        out_per_token = float(info.get("output_cost_per_token") or 0)
        cache_read_per_token = float(info.get("cache_read_input_token_cost") or 0)
        cache_write_per_token = float(info.get("cache_creation_input_token_cost") or 0)
        # Skip entries with no pricing at all — they're either local
        # models (Ollama, vLLM) or stub entries; either way, returning
        # them with all-zero prices clutters the frontend dropdowns.
        # The frontend's fallback shows $0 for unknown models which is
        # the correct presentation for local models too.
        if not any((in_per_token, out_per_token, cache_read_per_token, cache_write_per_token)):
            continue
        out[model_id] = {
            "input_per_mtok": in_per_token * 1_000_000,
            "output_per_mtok": out_per_token * 1_000_000,
            "cache_read_per_mtok": cache_read_per_token * 1_000_000,
            "cache_write_per_mtok": cache_write_per_token * 1_000_000,
        }
    return out


@router.get("/pricing")
async def get_pricing(
    _user: UserContext = Depends(get_current_user),
) -> dict:
    global _cached
    if _cached is None:
        _cached = {
            "source": "litellm",
            "currency": "USD",
            "unit": "per_million_tokens",
            "models": _build_pricing(),
        }
    return _cached
