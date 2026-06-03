"""Demo-mode helpers.

Today: a per-session turn counter for the /investigate endpoint so a
single demo visitor on the shared `demo` account can't burn the LLM
budget. The map is in-memory, keyed by session_id, and trimmed lazily
when it grows past a small bound — drift-agent restarts reset it
(intentional: fresh deploy = fresh budget).

Future:
- Daily / monthly total token cap, sourced from VM metrics so it
  survives drift-agent restarts.
- Per-IP rate limiting for the chat surface.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from .config import settings


# Trim threshold: when the map grows past this many entries, evict
# entries whose last-seen is older than 7 days. Keeps memory bounded
# even if a flood of one-shot demo sessions arrives without ever
# expiring (their cookies still live until logout / session table
# cleanup).
_MAX_TRACKED_SESSIONS = 5000
_EVICTION_AGE_SECONDS = 7 * 24 * 3600


@dataclass
class _SessionBudget:
    turns_used: int = 0
    last_seen: float = field(default_factory=time.time)


# Module-level state — single drift-agent process owns its own copy.
# That's fine for a demo: in a multi-worker setup the budget would be
# per-worker, which over-counts (good — bounded above the cap). For
# real isolation a future iteration would back this with redis.
_budgets: dict[uuid.UUID, _SessionBudget] = {}


def _evict_stale() -> None:
    """Drop entries older than _EVICTION_AGE_SECONDS. Called only when
    the map is too large to avoid scanning on every check."""
    cutoff = time.time() - _EVICTION_AGE_SECONDS
    stale = [sid for sid, b in _budgets.items() if b.last_seen < cutoff]
    for sid in stale:
        del _budgets[sid]


def remaining_turns(session_id: uuid.UUID) -> int:
    """How many investigation turns this session has left.
    Returns settings.demo_max_turns_per_session for an unseen session.
    Cheap: O(1). Never mutates state."""
    b = _budgets.get(session_id)
    used = b.turns_used if b else 0
    return max(settings.demo_max_turns_per_session - used, 0)


def consume_turn(session_id: uuid.UUID) -> int:
    """Increment the session's turn count and return the new
    remaining-budget. If the count would push the session over the
    cap, returns -1 WITHOUT incrementing (so the caller can refuse
    cleanly)."""
    if len(_budgets) > _MAX_TRACKED_SESSIONS:
        _evict_stale()
    b = _budgets.setdefault(session_id, _SessionBudget())
    if b.turns_used >= settings.demo_max_turns_per_session:
        return -1
    b.turns_used += 1
    b.last_seen = time.time()
    return settings.demo_max_turns_per_session - b.turns_used


def reset_session(session_id: uuid.UUID) -> None:
    """Wipe one session's turn counter. Intended for test fixtures
    and the nightly demo-reset cron job."""
    _budgets.pop(session_id, None)


def reset_all() -> None:
    """Wipe every tracked session. For the nightly reset job."""
    _budgets.clear()
