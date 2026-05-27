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
    # NO backfill from group_id — tags and group_id are intentionally
    # decoupled. group_id is the access-control unit (admins scoped to
    # groups); tags are filter metadata operators apply explicitly.
    # If we backfilled, deleting the auto-tag would create a confusing
    # mismatch: device still in group "home" but tag "home" missing,
    # so tag-filter for "home" skips a device that's literally in
    # group home. Cleaner to start tags empty and let operators tag
    # what they want.
    op.create_index(
        "ix_devices_tags",
        "devices",
        ["tags"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_devices_tags", table_name="devices")
    op.drop_column("devices", "tags")
