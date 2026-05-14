"""Pydantic request/response models for the Drift Deploy API.

These shapes are exchanged with two callers:
  - Drift's LLM tools (admin surface): apps, revisions, deployment targeting.
  - Edge agents (agent surface): bootstrap + check-in.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ---------- Devices ----------


class DeviceOut(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    last_seen: Optional[datetime] = None
    agent_version: Optional[str] = None
    created_at: datetime


class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class DeviceCreated(BaseModel):
    device: DeviceOut
    bootstrap_token: str  # one-time; not stored in cleartext after creation
    install_cmd: str      # ready-to-paste curl|sh for the operator


# ---------- Apps + revisions ----------


class AppOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime


class AppCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class AppRevisionOut(BaseModel):
    id: uuid.UUID
    app_id: uuid.UUID
    version: int
    bundle_url: Optional[str] = None
    bundle_sha256: Optional[str] = None
    created_at: datetime


COMPOSE_FILE_CANDIDATES = ("compose.yaml", "compose.yml", "docker-compose.yml")


class AppRevisionCreate(BaseModel):
    """A bundle is a flat map of filenames to contents. Must include one of
    the standard compose filenames; can also include .env and any files
    referenced by relative path from the compose file (e.g. prometheus.yml).
    """

    files: dict[str, str] = Field(min_length=1)

    def model_post_init(self, _ctx) -> None:  # type: ignore[override]
        # Reject path traversal + ensure a compose file exists.
        for name in self.files:
            if "/" in name or name.startswith("."):
                # Allow a literal .env but no leading-dot anywhere else.
                if name != ".env":
                    raise ValueError(f"file '{name}': only basenames are allowed, no paths or leading dots (except .env)")
        if not any(name in self.files for name in COMPOSE_FILE_CANDIDATES):
            raise ValueError(
                f"bundle must contain one of: {', '.join(COMPOSE_FILE_CANDIDATES)}"
            )


# ---------- Deployment targets ----------


class DeploymentTargetOut(BaseModel):
    id: uuid.UUID
    device_id: uuid.UUID
    app_id: uuid.UUID
    desired_revision_id: Optional[uuid.UUID]
    current_revision_id: Optional[uuid.UUID]
    status: str
    attempts: int
    last_error: Optional[str]
    updated_at: datetime


class DeploymentTargetSet(BaseModel):
    device_id: uuid.UUID
    app_id: uuid.UUID
    revision_id: uuid.UUID


# ---------- Agent surface ----------


class AgentCheckIn(BaseModel):
    """Submitted by the edge agent every ~30s."""

    device_name: str
    agent_version: str
    # Map of app_name → revision_id currently running on the device. May be
    # empty when the device has never applied anything yet.
    current_revisions: dict[str, uuid.UUID] = Field(default_factory=dict)
    health: dict = Field(default_factory=dict)


class DesiredApp(BaseModel):
    app: str
    revision_id: uuid.UUID
    bundle_url: str
    bundle_sha256: str


class AgentCheckInResponse(BaseModel):
    desired: list[DesiredApp] = Field(default_factory=list)


class AgentBootstrap(BaseModel):
    """First-contact request. Token authenticates; server returns device JWT in v1."""

    device_name: str
    bootstrap_token: str
    agent_version: str
