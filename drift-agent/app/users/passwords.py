"""bcrypt wrapper. Single place to set cost factor so we can rotate later.

12 is the current default cost; 13 doubles work. On modern hardware
12 takes ~150ms to hash, which is the right ballpark for an interactive
login latency budget.
"""
from __future__ import annotations

import bcrypt

_BCRYPT_ROUNDS = 12


def hash_password(plaintext: str) -> str:
    if not plaintext:
        raise ValueError("empty password")
    return bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode()


def verify_password(plaintext: str, stored_hash: str) -> bool:
    if not plaintext or not stored_hash:
        return False
    try:
        return bcrypt.checkpw(plaintext.encode(), stored_hash.encode())
    except ValueError:
        # Malformed hash in storage. Treat as authentication failure
        # rather than 500 — defense in depth against a corrupted row.
        return False
