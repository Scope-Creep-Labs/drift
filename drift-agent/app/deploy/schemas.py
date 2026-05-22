"""Pydantic request/response models for the Drift Deploy API.

These shapes are exchanged with two callers:
  - Drift's LLM tools (admin surface): apps, revisions, deployment targeting.
  - Edge agents (agent surface): bootstrap + check-in.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------- Devices ----------


class DeviceOut(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    last_seen: Optional[datetime] = None
    agent_version: Optional[str] = None
    group_id: Optional[str] = None
    # Free-form identity facts. Shape from the agent (v0.5.3+):
    # interfaces, hostname, arch, os, kernel, docker_version.
    # Null on pre-v0.5.3 devices until they next check in.
    facts: Optional[dict] = None
    created_at: datetime


class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    # Logical grouping the device will declare on its first check-in.
    # Required so the operator (the one calling create_device) is locked
    # into a group they have access to — the install command is then
    # rendered with this GROUP_ID baked in, preventing a deploy-role user
    # from inadvertently commissioning a device into a group they can't
    # then manage.
    group_id: str = Field(min_length=1, max_length=128)


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


class AppRevisionDetail(AppRevisionOut):
    """Same as AppRevisionOut but with the full file map. Used by the UI's
    edit-app modal to pre-populate from the latest revision. Listing
    endpoints stay on AppRevisionOut so we don't ship file blobs in bulk."""

    files: dict[str, str]


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
    max_retries: int
    last_error: Optional[str]
    updated_at: datetime


class DeploymentTargetSet(BaseModel):
    device_id: uuid.UUID
    app_id: uuid.UUID
    revision_id: uuid.UUID
    # Optional override of the per-target retry cap. If unset, falls back
    # to the existing row's value (on update) or the model default (on
    # first deploy, currently 5).
    max_retries: Optional[int] = Field(default=None, ge=1, le=100)


# ---------- Agent surface ----------


class AgentCheckIn(BaseModel):
    """Submitted by the edge agent every ~30s."""

    device_name: str
    agent_version: str
    # Map of app_name → revision_id currently running on the device. May be
    # empty when the device has never applied anything yet.
    current_revisions: dict[str, uuid.UUID] = Field(default_factory=dict)
    # Logical group this device belongs to (cloud, edge, drift_home, ...).
    # Stored on the device row so deploy-by-group can resolve targets.
    group_id: Optional[str] = None
    # Map of app_name → last apply error. Absent or empty means no
    # outstanding errors. Backend uses this to flip DeploymentTarget.status
    # to "failed" and surface last_error in list_deployments.
    apply_errors: dict[str, str] = Field(default_factory=dict)
    health: dict = Field(default_factory=dict)
    # Identity facts: interfaces / hostname / arch / os / kernel /
    # docker_version. Reported every ~10min (not every tick — these
    # change slowly). Absent → CP leaves device.facts unchanged.
    facts: Optional[dict] = None


class DesiredApp(BaseModel):
    app: str
    # "deploy" — agent should apply the named revision. revision_id /
    # bundle_url / bundle_sha256 are populated.
    # "remove" — agent should stop the running compose project for this
    # app and drop it from local state. The bundle fields are unused.
    action: Literal["deploy", "remove"] = "deploy"
    revision_id: Optional[uuid.UUID] = None
    bundle_url: Optional[str] = None
    bundle_sha256: Optional[str] = None


class AgentCheckInResponse(BaseModel):
    desired: list[DesiredApp] = Field(default_factory=list)
    # 12-char prefix of the canonical drift-deploy-agent.sh's SHA-256.
    # If the agent's running AGENT_SHA differs, it self-updates by exiting
    # cleanly; Docker's --restart unless-stopped brings the container back
    # and the bootstrapper at the top of the script fetches the latest.
    agent_target_sha: Optional[str] = None
    # Docker config.json auths map. Shape: {"ghcr.io": {"auth": "<b64>"}}.
    # Set by the CP from registry_credentials, decrypted per check-in. The
    # agent writes this verbatim under /root/.docker/config.json so the
    # CLI inside the agent container picks it up for compose pull. Empty
    # map = no creds configured; agent leaves the file alone (no clobber).
    registry_credentials: dict[str, dict[str, str]] = Field(default_factory=dict)
    # Terminal session ids that this device has waiting in the CP's
    # relay. The agent forks one terminal-bridge.py per id (passes the
    # bootstrap token through as the WS Authorization header). Empty
    # list on every check-in until an operator clicks "Terminal" in the
    # UI, which inserts a `pending` row in terminal_sessions.
    pending_sessions: list[uuid.UUID] = Field(default_factory=list)


class AgentBootstrap(BaseModel):
    """First-contact request. Token authenticates; server returns device JWT in v1."""

    device_name: str
    bootstrap_token: str
    agent_version: str


# ---------- Registry credentials ----------


class RegistryCredentialOut(BaseModel):
    """Returned to the operator. Password is intentionally not in this
    shape — once set, it can be updated (overwrite) or deleted, but never
    read back. Matches how the docker CLI surfaces stored credentials."""

    id: uuid.UUID
    registry: str
    group_id: str
    username: str
    created_at: datetime
    updated_at: datetime


class RegistryCredentialSet(BaseModel):
    """Upsert payload from the UI. The password is required even on
    'update' because the server never decrypts to compare — every PUT
    replaces both fields. Operators re-paste the PAT to change anything.

    `group_id` scopes the credential — devices only receive creds whose
    group_id matches their own. Same registry can appear once per group."""

    registry: str = Field(min_length=1, max_length=256)
    group_id: str = Field(min_length=1, max_length=128)
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=4096)
