"""tunnel_sessions

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-22 00:00:00

Session table for the subdomain-tunnel feature. Each row is one in-
flight tunnel from a browser at tunnel-<subdomain>.<base>/... through
the CP into the edge agent's localhost:<port>. Pure routing/audit
state — the CP holds the multiplexed WS in memory; this row is what
the Caddy on-demand-TLS ask hook checks before issuing a cert and what
the subdomain router looks up to resolve <subdomain> → device.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tunnel_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "device_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("devices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subdomain_token", sa.String(64), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_tunnel_sessions_device_id", "tunnel_sessions", ["device_id"])
    op.create_index("ix_tunnel_sessions_user_id", "tunnel_sessions", ["user_id"])
    op.create_index(
        "ix_tunnel_sessions_subdomain_token",
        "tunnel_sessions",
        ["subdomain_token"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_tunnel_sessions_subdomain_token", table_name="tunnel_sessions")
    op.drop_index("ix_tunnel_sessions_user_id", table_name="tunnel_sessions")
    op.drop_index("ix_tunnel_sessions_device_id", table_name="tunnel_sessions")
    op.drop_table("tunnel_sessions")
