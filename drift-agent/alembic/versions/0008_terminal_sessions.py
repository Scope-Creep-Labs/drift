"""terminal_sessions

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-22 00:00:00

Audit table for the web-terminal feature. Each row is one session
between a browser (logged-in user) and a device. Metadata only — no
keystrokes captured. Lifecycle is driven by the relay in the CP.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "terminal_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "device_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("devices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bytes_browser_to_agent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bytes_agent_to_browser", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_terminal_sessions_device_id", "terminal_sessions", ["device_id"]
    )
    op.create_index(
        "ix_terminal_sessions_user_id", "terminal_sessions", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_terminal_sessions_user_id", table_name="terminal_sessions")
    op.drop_index("ix_terminal_sessions_device_id", table_name="terminal_sessions")
    op.drop_table("terminal_sessions")
