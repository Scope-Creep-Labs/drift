"""Normalization + filtering helpers for device tags.

Tags are free-form strings, but all writes go through `normalize_tags()`
so an operator typing `Edge`, ` edge `, or `EDGE` ends up with the same
canonical tag `edge`. This keeps tag-based filters predictable and
avoids the "why doesn't my filter match?" footgun.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from sqlalchemy.dialects.postgresql import JSONB

# Maximum string length per tag — keeps the JSONB column bounded and
# matches the column width we'd use if we ever migrated to a join table.
MAX_TAG_LEN = 64


def normalize_tag(raw: str) -> str:
    """Lowercase + strip a single tag. Returns "" if the result is
    empty (caller decides whether to skip or surface as an error)."""
    if not isinstance(raw, str):
        return ""
    s = raw.strip().lower()
    if not s:
        return ""
    if len(s) > MAX_TAG_LEN:
        s = s[:MAX_TAG_LEN]
    return s


def normalize_tags(raw_tags: Iterable[str] | None) -> list[str]:
    """Normalize + dedupe a tag list, preserving the first-seen order
    (so the canonical form remains predictable for snapshot diffs).
    Empty strings and non-strings are dropped silently."""
    if not raw_tags:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in raw_tags:
        n = normalize_tag(raw)
        if not n or n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


def tags_match_all(device_tags: Sequence[str] | None, required: Sequence[str]) -> bool:
    """In-memory match: device has ALL of the required tags. Use this
    when you've already loaded the devices; for DB-side filtering, use
    `tag_filter_clause` below.

    Match-all semantics make compositional filters intuitive:
        deploy reporter to devices with tags edge,client-z
        → match devices where tags ⊇ {edge, client-z}
    """
    if not required:
        return True
    have = set(device_tags or [])
    return all(t in have for t in required)


def tag_filter_clause(model_column, required: Sequence[str]):
    """SQLAlchemy expression: device_tags @> '[<required>]'::jsonb.

    JSONB `@>` is the containment operator — `[1,2,3] @> [2,3]` is
    true. With the GIN index on the column, queries hit the index for
    a fast scan. Returns None when `required` is empty so callers can
    short-circuit instead of building a no-op clause.
    """
    if not required:
        return None
    return model_column.op("@>")(list(required))  # cast handled by JSONB
