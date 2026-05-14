"""Bootstrap token generation + storage hashing.

v0 model (intentionally simple): the bootstrap token is the long-lived
device credential. Sent as `Authorization: Bearer <token>` on every call.
Server keeps only a SHA-256 of the token (fast comparison, never displayed
again). v1 will rotate to short-lived device JWTs.
"""
from __future__ import annotations

import hashlib
import secrets


def generate_bootstrap_token() -> str:
    """32 bytes of entropy, URL-safe — `drift-` prefix is purely cosmetic."""
    return "drift-" + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(token: str, expected_hash: str) -> bool:
    return secrets.compare_digest(hash_token(token), expected_hash)
