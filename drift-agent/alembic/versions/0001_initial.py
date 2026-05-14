"""initial drift-deploy schema

Revision ID: 0001
Revises:
Create Date: 2026-05-14 00:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "devices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("bootstrap_token_hash", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("agent_version", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "apps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "app_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("app_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("apps.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        # filename -> contents; must include a compose.yaml. See schemas.AppRevisionCreate.
        sa.Column("files", postgresql.JSONB, nullable=False),
        sa.Column("bundle_url", sa.String(1024), nullable=True),
        sa.Column("bundle_sha256", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("app_id", "version", name="uq_app_revision_version"),
    )
    op.create_table(
        "deployment_targets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("app_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("apps.id", ondelete="CASCADE"), nullable=False),
        sa.Column("desired_revision_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app_revisions.id"), nullable=True),
        sa.Column("current_revision_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app_revisions.id"), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("device_id", "app_id", name="uq_deployment_target"),
    )


def downgrade() -> None:
    op.drop_table("deployment_targets")
    op.drop_table("app_revisions")
    op.drop_table("apps")
    op.drop_table("devices")
