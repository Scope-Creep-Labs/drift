"""Symmetric encryption for stored credentials.

Uses Fernet (AES-128-CBC + HMAC-SHA-256) with a single key from
DRIFT_SECRET_KEY. The key never leaves the control plane; ciphertext sits
in Postgres so a DB dump alone isn't enough to leak credentials.

Failure mode: if DRIFT_SECRET_KEY is unset or invalid, every encrypt/
decrypt call raises and the surrounding endpoint returns 503. The
secrets_enabled property on Settings gates this earlier.
"""
from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from ..config import settings


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = settings.drift_secret_key
    if not key:
        raise RuntimeError("DRIFT_SECRET_KEY is not set; cannot encrypt/decrypt secrets")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as e:
        raise RuntimeError(f"DRIFT_SECRET_KEY is not a valid Fernet key: {e}")


def encrypt(plaintext: str) -> str:
    """Encrypt a plaintext secret. Returns a urlsafe-base64 string suitable
    for storing in a TEXT column."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt a previously encrypt()'d ciphertext. Raises if the key has
    rotated or the data is corrupt."""
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise RuntimeError(
            f"Could not decrypt secret — DRIFT_SECRET_KEY may have rotated or DB is corrupt: {e}"
        )
