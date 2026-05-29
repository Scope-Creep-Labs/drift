"""Login-attempt rate limiter (in-memory, per-process, sliding window).

Failed credential checks are tracked in two independent keyspaces:

- `user:<username>` — guards against a slow attacker grinding a single
  account's password. Lower threshold (a real user shouldn't fat-finger
  more than a handful of times).
- `ip:<client-ip>` — guards against credential-stuffing across many
  usernames from one source. Higher threshold so an office NAT with
  shared egress doesn't lock out legitimate retries.

Both windows are checked before bcrypt verify on every login attempt;
either hitting its threshold returns HTTP 429 and skips bcrypt entirely.
A successful login clears the username's tally (but never the IP's, so
one correct guess doesn't reset network-wide enforcement).

In-memory was chosen over Postgres or Redis because Drift runs one
drift-agent process per CP. The footprint is bounded by recent
activity; idle keys age out lazily on next access (no background
sweeper). At the deploy's expected scale (handfuls of users, dozens of
IPs over a 15-minute window) the dict has at most low-hundreds of
entries.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque


class LoginRateLimiter:
    """Sliding-window failure counter keyed by string.

    Two thresholds: one per username, one per IP. Both share the same
    time window so the operator only has one tunable number to reason
    about ("how long is the lockout / forget-period").
    """

    def __init__(
        self,
        *,
        max_per_username: int,
        max_per_ip: int,
        window_seconds: int,
    ) -> None:
        self._max_user = max_per_username
        self._max_ip = max_per_ip
        self._window = window_seconds
        # Single dict, prefix-keyed. Lock keeps the prune+append
        # critical section atomic across coroutines on a single
        # event loop (we don't span processes).
        self._buckets: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    def _max_for(self, key: str) -> int:
        # Prefix decides which threshold applies. Keeps the API simple:
        # the caller just hands us prefixed keys and the limiter knows
        # which budget to apply.
        if key.startswith("user:"):
            return self._max_user
        return self._max_ip

    def _prune(self, bucket: deque[float], now: float) -> None:
        cutoff = now - self._window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

    async def is_locked(self, key: str) -> bool:
        now = time.monotonic()
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return False
            self._prune(bucket, now)
            if not bucket:
                # Bucket aged out completely; drop the key so idle
                # users don't keep dict entries alive forever.
                del self._buckets[key]
                return False
            return len(bucket) >= self._max_for(key)

    async def record_failure(self, key: str) -> int:
        """Append a failure timestamp. Returns the post-prune count
        so the caller can decide if this attempt should already be
        rejected before bcrypt (it isn't, today — we let the user see
        a single 401 on the last allowed attempt and the 429 only on
        the NEXT one)."""
        now = time.monotonic()
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = deque()
                self._buckets[key] = bucket
            self._prune(bucket, now)
            bucket.append(now)
            return len(bucket)

    async def clear(self, key: str) -> None:
        """Drop the bucket. Used on successful login to clear the
        username's tally — the user proved they're real, give them a
        clean slate."""
        async with self._lock:
            self._buckets.pop(key, None)


# Module-level singleton. Bound to settings on first access so the
# `Settings()` instance is fully constructed; trying to import settings
# at module scope races with FastAPI's app-startup ordering.
_limiter: LoginRateLimiter | None = None


def get_login_limiter() -> LoginRateLimiter:
    global _limiter
    if _limiter is None:
        # Lazy import to avoid an import cycle: config imports nothing
        # from users; users importing config at module scope wouldn't
        # cycle but does mean the limiter's params freeze at first
        # access, which is what we want.
        from ..config import settings
        _limiter = LoginRateLimiter(
            max_per_username=settings.login_max_failures_per_username,
            max_per_ip=settings.login_max_failures_per_ip,
            window_seconds=settings.login_failure_window_seconds,
        )
    return _limiter


def client_ip_from_request(request) -> str:
    """Best-effort source IP. Reads `X-Forwarded-For` first (Caddy /
    nginx in front of uvicorn add it); falls back to the immediate
    socket peer (which is the proxy's IP behind a reverse proxy, the
    real client otherwise).

    Spoofing X-Forwarded-For doesn't bypass rate limiting — it just
    means the attacker is rate-limited per spoofed value. They'd still
    hit the per-username threshold even if every request claims a
    different IP.

    Takes the leftmost hop of XFF (the original client) per the
    convention `X-Forwarded-For: client, proxy1, proxy2`. We don't
    validate that intermediate hops are trusted because we're not
    making security decisions on the value; rate-limit bucketing is
    forgiving of imperfect inputs.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",", 1)[0].strip()
        if first:
            return first
    client = getattr(request, "client", None)
    if client is not None and client.host:
        return client.host
    # uvicorn always sets request.client; the fallback is defensive
    # against test contexts that don't.
    return "unknown"
