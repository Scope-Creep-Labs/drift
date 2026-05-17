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
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    # bcrypt hash of the bootstrap token; cleared after first successful check-in.
    bootstrap_token_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    last_seen: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    agent_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Logical group reported by the agent on check-in. Used for fleet-wide
    # operations like deploy_revision_to_group.
    group_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
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
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, onupdate=_now_utc, nullable=False
    )


class RegistryCredential(Base):
    """Per-registry pull credentials. password_encrypted is Fernet
    ciphertext (see deploy.secrets). One row per registry — operator
    sets it once via the UI, every device picks it up at its next
    check-in (as a docker config.json auths entry)."""

    __tablename__ = "registry_credentials"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # "ghcr.io", "docker.io", "registry.gitlab.com", … whatever the
    # auths-key of docker config.json expects. Unique so upsert by name.
    registry: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    username: Mapped[str] = mapped_column(String(256), nullable=False)
    password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, onupdate=_now_utc, nullable=False
    )
