"""Device-name normalization.

All operator-supplied device names (commission, lookup, delete, tag,
deploy, terminal) flow through `normalize_device_name()` so that
casing and whitespace differences resolve to the same row. The DB
stores the normalized form; the case-insensitive partial unique index
in migration 0011 backstops the application-level normalization.

Same shape as `tagging.normalize_tag` — lowercase + strip — kept in a
separate module to make the device-name vs tag distinction obvious in
imports.
"""

from __future__ import annotations


MAX_DEVICE_NAME_LEN = 128


def normalize_device_name(raw: str | None) -> str:
    """Lowercase + strip. Returns "" if the input is empty/None/non-str
    so callers can return a clear validation error instead of trying
    to look up the empty string."""
    if not isinstance(raw, str):
        return ""
    s = raw.strip().lower()
    if len(s) > MAX_DEVICE_NAME_LEN:
        s = s[:MAX_DEVICE_NAME_LEN]
    return s
