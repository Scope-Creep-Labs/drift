"""Compose bundle packaging + B2 (S3-compatible) upload.

A bundle is a tar.gz containing `compose.yaml` and optional `.env`. The
edge agent downloads it, verifies sha256, extracts to a per-revision
directory, and runs `docker compose up -d`.

Stays import-safe when B2 isn't configured: the boto3 client is created
lazily inside upload_bundle().
"""
from __future__ import annotations

import hashlib
import io
import tarfile
import uuid
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.client import Config

from ..config import settings


def pack(files: dict[str, str]) -> tuple[bytes, str]:
    """Build a tar.gz of the given filename→contents map. Returns (bytes, sha256hex).

    Files land at the archive root, so on extraction they sit next to each
    other and `docker compose up -d` (run from that dir) resolves any
    relative bind-mounts (e.g. `./prometheus.yml`) against the bundle.
    """
    buf = io.BytesIO()
    # Sort keys so the resulting bytes are deterministic for the same input.
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in sorted(files):
            _add_str(tar, name, files[name])
    data = buf.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    return data, digest


def _add_str(tar: tarfile.TarFile, name: str, content: str) -> None:
    raw = content.encode("utf-8")
    info = tarfile.TarInfo(name=name)
    info.size = len(raw)
    info.mtime = int(datetime.now(timezone.utc).timestamp())
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(raw))


def _client():
    if not (settings.b2_endpoint and settings.b2_access_key_id and settings.b2_bucket):
        raise RuntimeError("B2 storage not configured (B2_ENDPOINT / B2_BUCKET / credentials)")
    return boto3.client(
        "s3",
        endpoint_url=settings.b2_endpoint,
        region_name=settings.b2_region or None,
        aws_access_key_id=settings.b2_access_key_id,
        aws_secret_access_key=settings.b2_secret_access_key,
        config=Config(signature_version="s3v4"),
    )


def upload_bundle(app_name: str, revision_version: int, body: bytes) -> str:
    """Upload to B2; return the s3:// URL we store in the DB row."""
    key = f"{settings.b2_prefix.rstrip('/')}/{app_name}/v{revision_version}-{uuid.uuid4().hex[:8]}.tar.gz"
    c = _client()
    c.put_object(Bucket=settings.b2_bucket, Key=key, Body=body, ContentType="application/gzip")
    return f"s3://{settings.b2_bucket}/{key}"


def presign_get(s3_url: str, expires_in: int = 600) -> str:
    """Convert an s3://bucket/key URL into a presigned HTTPS GET for the agent."""
    if not s3_url.startswith("s3://"):
        raise ValueError(f"not an s3:// url: {s3_url}")
    _, _, rest = s3_url.partition("s3://")
    bucket, _, key = rest.partition("/")
    c = _client()
    return c.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_in,
    )
