"""device.group_id

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-15 02:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Logical grouping the device reports on check-in (cloud, edge,
    # drift_home, client-acme, ...). Used by deploy-by-group routing.
    op.add_column(
        "devices",
        sa.Column("group_id", sa.String(128), nullable=True),
    )
    op.create_index("ix_devices_group_id", "devices", ["group_id"])


def downgrade() -> None:
    op.drop_index("ix_devices_group_id", table_name="devices")
    op.drop_column("devices", "group_id")
