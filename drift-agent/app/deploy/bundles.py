"""Compose bundle packaging + storage backends.

A bundle is a tar.gz containing `compose.yaml` and optional `.env`. The
edge agent downloads it, gunzips, verifies the sha256 of the inner tar,
extracts, runs `docker compose up -d`.

sha256 is of the UNCOMPRESSED tar (not the gzip) — gzip output isn't
byte-stable across implementations / compression levels / dates, so
verifying the inner tar gives us a deterministic, transport-agnostic
fingerprint.

Two backends, switched by `settings.bundle_storage`:

  local (default)  Bundles live on the CP's filesystem under
                   BUNDLE_STORAGE_PATH. drift-agent serves them via
                   /api/deploy/agent/bundles/<filename> (bearer-token
                   gated like every other agent endpoint). bundle_url
                   in the DB is `local:<filename>`.

  s3               Bundles upload to a B2/S3-compatible bucket. Agents
                   download via short-lived presigned URLs. bundle_url
                   is `s3://bucket/key`.

The check-in handler converts the stored URL to an agent-facing string
just before emitting it in DesiredApp.
"""
from __future__ import annotations

import hashlib
import io
import os
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from ..config import settings


# ---------- packing ----------


def pack(files: dict[str, str]) -> tuple[bytes, str]:
    """Build a tar.gz of the given filename→contents map.

    Returns (gz_bytes, tar_sha256_hex). The sha256 is of the UNCOMPRESSED
    tar so verification is gzip-implementation-independent.
    """
    # First write a deterministic tar.
    tar_buf = io.BytesIO()
    # mtime is fixed so two packs of the same input produce byte-identical
    # tarballs (and thus identical sha256s). The actual timestamp is
    # tracked separately in the AppRevision row.
    fixed_mtime = 0
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        for name in sorted(files):
            raw = files[name].encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(raw)
            info.mtime = fixed_mtime
            info.mode = 0o644
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            tar.addfile(info, io.BytesIO(raw))
    tar_bytes = tar_buf.getvalue()
    tar_sha = hashlib.sha256(tar_bytes).hexdigest()

    # Then gzip it. The gzip layer's bytes vary across implementations
    # but we don't care — we verify the inner tar on the edge.
    import gzip
    gz_buf = io.BytesIO()
    with gzip.GzipFile(fileobj=gz_buf, mode="wb", mtime=0) as gz:
        gz.write(tar_bytes)
    return gz_buf.getvalue(), tar_sha


# ---------- backends ----------


class BundleBackend(Protocol):
    """Abstract storage backend. `upload` stores; `to_agent_url` converts
    the stored bundle_url to something the edge agent can fetch."""

    def upload(self, app_name: str, revision_version: int, gz_bytes: bytes) -> str: ...
    def to_agent_url(self, stored_url: str) -> str: ...
    def open_local(self, filename: str) -> bytes: ...


class LocalBackend:
    """Bundles live as files in `settings.bundle_storage_path`. The CP
    serves them via /api/deploy/agent/bundles/<filename>; the bearer-
    token gate is the existing per-device check-in auth.

    `stored_url` format: `local:<filename>` so the same DB column works
    for both backends.
    """

    def __init__(self) -> None:
        self.root = Path(settings.bundle_storage_path)
        # Don't mkdir at instantiation — the path may not be writable
        # in test/dev contexts where the backend is just being probed.
        # We ensure-create on actual upload + reads.

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def upload(self, app_name: str, revision_version: int, gz_bytes: bytes) -> str:
        self._ensure_root()
        filename = f"{app_name}-v{revision_version}-{uuid.uuid4().hex[:8]}.tar.gz"
        path = self.root / filename
        # Atomic write: temp file in same dir → rename. Avoids serving
        # a half-written bundle if the CP crashes mid-upload.
        tmp = path.with_suffix(".tar.gz.tmp")
        tmp.write_bytes(gz_bytes)
        os.chmod(tmp, 0o640)
        tmp.replace(path)
        return f"local:{filename}"

    def to_agent_url(self, stored_url: str) -> str:
        # The edge agent prepends ${CP_URL} when it sees the `local:`
        # prefix. The CP doesn't know its own public URL, and the agent
        # already has CP_URL in /etc/drift-deploy/env, so this is the
        # cleanest split of responsibility.
        return stored_url

    def open_local(self, filename: str) -> bytes:
        # Defensive: refuse traversal even though FastAPI's path param
        # validation should already block "..".
        if "/" in filename or ".." in filename:
            raise ValueError(f"invalid bundle filename: {filename}")
        path = self.root / filename
        if not path.is_file():
            raise FileNotFoundError(f"bundle not found: {filename}")
        return path.read_bytes()


class S3Backend:
    """B2 / S3 backend. Bundles upload via boto3; agents download via
    short-lived presigned URLs."""

    def __init__(self) -> None:
        if not (settings.b2_endpoint and settings.b2_access_key_id and settings.b2_bucket):
            raise RuntimeError("S3 storage selected but B2_ENDPOINT / B2_BUCKET / credentials missing")

    def _client(self):
        import boto3
        from botocore.client import Config
        return boto3.client(
            "s3",
            endpoint_url=settings.b2_endpoint,
            region_name=settings.b2_region or None,
            aws_access_key_id=settings.b2_access_key_id,
            aws_secret_access_key=settings.b2_secret_access_key,
            config=Config(signature_version="s3v4"),
        )

    def upload(self, app_name: str, revision_version: int, gz_bytes: bytes) -> str:
        key = f"{settings.b2_prefix.rstrip('/')}/{app_name}/v{revision_version}-{uuid.uuid4().hex[:8]}.tar.gz"
        self._client().put_object(
            Bucket=settings.b2_bucket,
            Key=key,
            Body=gz_bytes,
            ContentType="application/gzip",
        )
        return f"s3://{settings.b2_bucket}/{key}"

    def to_agent_url(self, stored_url: str) -> str:
        # Presigned HTTPS URL valid for 10 minutes.
        if not stored_url.startswith("s3://"):
            raise ValueError(f"not an s3:// url: {stored_url}")
        _, _, rest = stored_url.partition("s3://")
        bucket, _, key = rest.partition("/")
        return self._client().generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=600,
        )

    def open_local(self, filename: str) -> bytes:
        raise NotImplementedError("S3Backend doesn't serve bundles via the CP")


def get_backend() -> BundleBackend:
    storage = (settings.bundle_storage or "local").lower()
    if storage == "s3":
        return S3Backend()
    return LocalBackend()


# ---------- back-compat shims (used by existing callers) ----------


def upload_bundle(app_name: str, revision_version: int, gz_bytes: bytes) -> str:
    """Back-compat wrapper. New callers should use get_backend().upload()."""
    return get_backend().upload(app_name, revision_version, gz_bytes)


def presign_get(stored_url: str, expires_in: int = 600) -> str:
    """Back-compat wrapper. Resolves to the agent-facing URL via the
    backend that owns the stored_url's scheme."""
    if stored_url.startswith("s3://"):
        return S3Backend().to_agent_url(stored_url)
    if stored_url.startswith("local:"):
        return LocalBackend().to_agent_url(stored_url)
    raise ValueError(f"unrecognized bundle_url scheme: {stored_url}")
