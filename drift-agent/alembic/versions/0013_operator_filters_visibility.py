"""operator_filters.visibility — per-user vs fleet-wide rules

Adds a `visibility` column (`'private' | 'fleet'`) so any operator can
promote a personal filter to fleet-wide visibility. Default 'private'
preserves the v0.1.47 semantics for existing rows.

Lookup semantics after this column lands:
- list_relevant_filters returns (visibility='private' AND user_id=me)
  OR visibility='fleet'.
- promote_filter flips visibility 'private' → 'fleet' on a filter the
  caller owns; refuses if an identical fleet filter already exists
  (dedup).
- forget_filter still hard-deletes the row, but only if owned by the
  caller — fleet filters created by other operators are read-only to
  you.

Schema as a simple VARCHAR (no enum type) — matches the rest of the
codebase (status fields on devices, deployment_targets, etc.) which
all use varchar for cheap migrations.

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-01 18:30:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "operator_filters",
        sa.Column(
            "visibility",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'private'"),
        ),
    )
    # Cheap composite index for the common list query: per-user filters
    # AND public fleet filters in one scan.
    op.create_index(
        "ix_operator_filters_visibility",
        "operator_filters",
        ["visibility"],
    )


def downgrade() -> None:
    op.drop_index("ix_operator_filters_visibility", table_name="operator_filters")
    op.drop_column("operator_filters", "visibility")
