"""devices.tags JSONB array

Free-form string tags on each device, used by the chat agent + UI for
multi-dimensional filtering ("deploy reporter to all edge devices for
client-z"). Complements `group_id` (which stays the primary
access-control unit); tags are additional metadata.

Backfill: every device's `group_id` becomes a default initial tag so
existing filter targets still work.

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-27 00:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    # Backfill: seed each device's tags from its group_id so legacy
    # group-targeted commands still work via tag-based filters.
    op.execute(
        """
        UPDATE devices
           SET tags = jsonb_build_array(group_id)
         WHERE group_id IS NOT NULL
           AND group_id <> ''
           AND tags = '[]'::jsonb
        """
    )
    # GIN index for fast "tag in array" queries: tags @> '["edge"]'
    # uses this index. Most operator filters touch 1-3 tags so the
    # index pays off quickly.
    op.create_index(
        "ix_devices_tags",
        "devices",
        ["tags"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_devices_tags", table_name="devices")
    op.drop_column("devices", "tags")
