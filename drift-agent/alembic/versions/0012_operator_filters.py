"""operator_filters — per-user noise-suppression rules

Adds a single table backing the remember_filter / list_relevant_filters /
forget_filter agent tools. The investigation agent calls these to learn
operator-supplied "ignore this recurring error" rules and apply them in
future investigations scoped to the same device / container / group.

Schema choices:

- user_id FK with ON DELETE CASCADE: filters are personal; removing a
  user removes their filters with no orphan trace.
- scope as JSONB: the relevant keys are sparse and may grow over time
  (today: device/container/group/signal). Flat columns would mean a
  migration every time we add a new dimension; JSONB keeps the schema
  stable.
- pattern as plain TEXT: matched as a case-insensitive substring at
  read time. No regex / wildcards in v1 — keeps the surface area small
  and avoids ReDoS risk.
- apply_count + last_applied_at: lightweight usefulness signal. Lets
  the operator (or a future "stale filter" prompt) spot rules that
  haven't fired in a long time and prune them.

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-01 00:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "operator_filters",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("pattern", sa.Text, nullable=False),
        sa.Column(
            "scope",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "reason",
            sa.Text,
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "apply_count",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_index("ix_operator_filters_user_id", "operator_filters", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_operator_filters_user_id", table_name="operator_filters")
    op.drop_table("operator_filters")
