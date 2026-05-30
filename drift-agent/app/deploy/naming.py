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


# Docker Compose project-name rules: must consist only of lowercase
# alphanumeric, hyphens, and underscores, and must start with a letter
# or number. App names ARE the compose project name on each device, so
# we reject creation of anything compose would later refuse.
import re as _re
_APP_NAME_RE = _re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")


def validate_app_name(raw: str | None) -> str | None:
    """Return None if the name is a valid compose project name,
    otherwise an operator-readable error string explaining the rule.

    The actual `docker compose -p <name>` call rejects names that don't
    match `^[a-z0-9][a-z0-9_-]*$` at runtime — we replicate the same
    check at creation time so the operator gets a clear error before
    any deployment target ever exists."""
    if not isinstance(raw, str):
        return "name is required"
    s = raw.strip()
    if not s:
        return "name is required"
    if len(s) > 128:
        return "name is too long (max 128 chars)"
    if _APP_NAME_RE.match(s):
        return None
    # Build a helpful message that pinpoints what specifically broke
    # the rule, since "invalid name" without specifics is frustrating.
    reasons: list[str] = []
    if any(c.isupper() for c in s):
        reasons.append("contains uppercase letters")
    if s[0] in "-_":
        reasons.append("starts with a hyphen or underscore")
    if any(not (c.isalnum() or c in "-_") for c in s):
        reasons.append("contains characters other than letters, digits, hyphens, underscores")
    detail = "; ".join(reasons) if reasons else "doesn't match [a-z0-9][a-z0-9_-]*"
    return (
        f"app name '{raw}' is invalid: {detail}. "
        "Compose project names must be lowercase alphanumeric + hyphens/underscores, "
        "starting with a letter or number. Examples: 'node-red', 'podnot', 'my_app2'."
    )
