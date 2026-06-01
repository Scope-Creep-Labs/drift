"""operator_filter_mutes — per-user opt-out for any filter

Adds a join table letting any operator suppress the APPLICATION of a
filter for themselves without removing the row. Works on every filter
type:

- own private  → mute = "keep the row, stop applying it this debugging
                 session". Saves the delete-and-recreate dance for
                 short-lived opt-outs.
- own fleet    → mute = "I promoted this for the team but want raw data
                 in my own investigations". Other operators unaffected.
- others' fleet → mute is the only way to opt out (you can't delete a
                 filter someone else created).

Composite PK (user_id, filter_id) makes mute idempotent and lookups
fast. CASCADE FKs on both sides — deleting the user OR the filter row
cleans the mute rows automatically with no orphan.

list_relevant_filters (agent tool) excludes muted filters from its
return. /api/filters (sidebar list) still SHOWS muted filters but tags
them with `muted_by_me: true` so the UI can render a "muted" chip and
flip the toggle.

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-01 19:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "operator_filter_mutes",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "filter_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("operator_filters.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    # FK side index — the "who muted this filter" / cascade-cleanup
    # query goes through filter_id; the composite PK already covers
    # the (user_id, ...) direction.
    op.create_index(
        "ix_operator_filter_mutes_filter_id",
        "operator_filter_mutes",
        ["filter_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_operator_filter_mutes_filter_id", table_name="operator_filter_mutes")
    op.drop_table("operator_filter_mutes")
