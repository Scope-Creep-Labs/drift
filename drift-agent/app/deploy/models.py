"""SQLAlchemy ORM models for Drift Deploy (v0 schema).

Four tables for v0: devices, apps, app_revisions, deployment_targets.
Groups, audit log, and signed-bundle tracking come in v1.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    String,
    Integer,
    Text,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Always stored in normalized form (lowercase + stripped). Uniqueness
    # is enforced by a partial unique index on LOWER(name) WHERE status !=
    # 'removed' (see migration 0011) — case-insensitive at the DB level
    # AND lenient toward removed-status tombstones so a freed name is
    # reusable. All write paths normalize via naming.normalize_device_name.
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # bcrypt hash of the per-device bootstrap token. Long-lived bearer
    # credential the edge agent presents on every /agent/check-in; stays
    # valid for the life of the device row. Save the curl line to a
    # password manager — the chat won't render it again.
    bootstrap_token_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # SHA-256 hex of the host's /etc/machine-id (with fallback chain),
    # captured TOFU on the first check-in after commissioning. Subsequent
    # check-ins must match; mismatch returns 409 so an accidental
    # cross-host paste of the commissioning curl fails loudly instead of
    # silently flipping device state between two machines. Cleared
    # implicitly only by deleting + re-commissioning the device under a
    # new name. NULL on pre-v0.11 devices until they next check in.
    host_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    last_seen: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    agent_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Logical group reported by the agent on check-in. Used for fleet-wide
    # operations like deploy_revision_to_group.
    group_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    # Free-form identity facts reported by the agent: interfaces, hostname,
    # arch, os, kernel, docker_version. Overwritten on check-in (the
    # latest snapshot wins). For operational metrics (disk, mem, uptime)
    # use the time-series in VictoriaMetrics via node-exporter.
    facts: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # Free-form normalized string tags. All writes go through
    # `normalize_tags()` in deploy/tagging.py: lowercase, strip, dedupe.
    # Operator-facing "deploy reporter to all edge devices for client-z"
    # → tags filter ["edge", "client-z"], match-all semantics.
    tags: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, nullable=False)


class App(Base):
    __tablename__ = "apps"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, nullable=False)

    revisions: Mapped[list["AppRevision"]] = relationship(
        back_populates="app", cascade="all, delete-orphan"
    )


class AppRevision(Base):
    __tablename__ = "app_revisions"
    __table_args__ = (UniqueConstraint("app_id", "version", name="uq_app_revision_version"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    app_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("apps.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # filename → contents. Must contain a compose file (compose.yaml /
    # compose.yml / docker-compose.yml). May also contain .env and any
    # files referenced by relative paths from the compose file.
    files: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    bundle_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    bundle_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, nullable=False)

    app: Mapped[App] = relationship(back_populates="revisions")


class DeploymentTarget(Base):
    """Desired + observed state for one (device, app) pair."""

    __tablename__ = "deployment_targets"
    __table_args__ = (
        UniqueConstraint("device_id", "app_id", name="uq_deployment_target"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    app_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("apps.id", ondelete="CASCADE"), nullable=False
    )
    desired_revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_revisions.id"), nullable=True
    )
    current_revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_revisions.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Hard cap on consecutive apply failures before the target is paused.
    # When attempts hits this value, status flips to 'paused_retries' and
    # the CP stops shipping the bundle to the agent — operator has to
    # explicitly resume (retry_deployment) or push a new desired revision
    # to retry. Default 5 = ~75–150s of retrying at POLL_INTERVAL=15–30s.
    max_retries: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # One-shot restart signal. Set by the LLM tools / admin route to
    # tell the agent to `docker compose restart` this app's containers
    # without re-pulling the bundle or recreating containers. The CP
    # surfaces this as a DesiredApp(action='restart') on the next
    # check-in and clears the flag immediately (optimistic — if the
    # operator wants idempotent retries on failure, they re-issue).
    pending_restart: Mapped[bool] = mapped_column(default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, onupdate=_now_utc, nullable=False
    )


class User(Base):
    """An operator account. Authenticates with username + bcrypt password.
    Role determines coarse capability tier; user_groups (separate table)
    determines which device groups they can act on.

    Roles form a strict containment order:
      observe ⊂ deploy ⊂ admin
    Permission checks use >= so anything `observe` can do, `deploy` can
    do too. Alert-management tools intentionally require only `observe`
    since the observability domain encompasses alert configuration.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    # 'observe' | 'deploy' | 'admin'. Validated at the app layer; no enum
    # constraint at the DB to keep migrations cheap.
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="observe")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, nullable=False
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class UserGroup(Base):
    """Join table: which device groups can a user manage. Composite PK so
    re-assigning is an idempotent upsert. Admin users get implicit access
    to all groups regardless of this table's contents (checked in the
    auth layer)."""

    __tablename__ = "user_groups"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[str] = mapped_column(String(128), primary_key=True)


class Session(Base):
    """Opaque session token → user mapping. Cookie value is the session
    id (uuid4). expires_at is bumped on each authenticated request to
    keep active users logged in; idle sessions cleaned up by a periodic
    job (or just left to age — we look at expires_at on every check)."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, nullable=False
    )


class TerminalSession(Base):
    """A web-terminal session against a device. Lifecycle:
      - pending: row created by POST /devices/{name}/terminal; agent
        will pick it up on its next check-in.
      - active:  both browser and agent WS have attached and the CP
        is relaying bytes.
      - closed:  either side disconnected cleanly.
      - expired: no agent attached within `pending_timeout_seconds`
        OR session exceeded `max_session_seconds`.

    Bytes counters are best-effort — they tick every flush from the
    relay and let us spot one-way silence in audit without paying for
    keystroke capture.
    """

    __tablename__ = "terminal_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("devices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # FK to users.id; users live in a separate model (auth subsystem),
    # we don't relationship() across to avoid a deploy ↔ auth circular.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, nullable=False
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    bytes_browser_to_agent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bytes_agent_to_browser: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class RegistryCredential(Base):
    """Per-registry pull credentials, scoped to a group. password_encrypted
    is Fernet ciphertext (see deploy.secrets). The same registry can have
    different creds in different groups (so e.g. group=cloud can use one
    ghcr.io account and group=client-x can use another); uniqueness is on
    the (registry, group_id) pair. Only devices whose group_id matches a
    row's group_id receive it at check-in."""

    __tablename__ = "registry_credentials"
    __table_args__ = (
        UniqueConstraint("registry", "group_id", name="uq_registry_credentials_registry_group"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # "ghcr.io", "docker.io", "registry.gitlab.com", … whatever the
    # auths-key of docker config.json expects.
    registry: Mapped[str] = mapped_column(String(256), nullable=False)
    # Free-form group identifier. Matches Device.group_id at check-in time;
    # no FK because groups are pure strings (no group table).
    group_id: Mapped[str] = mapped_column(String(128), nullable=False)
    username: Mapped[str] = mapped_column(String(256), nullable=False)
    password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, onupdate=_now_utc, nullable=False
    )
