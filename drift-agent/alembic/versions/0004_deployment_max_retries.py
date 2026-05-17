"""deployment_targets.max_retries

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-17 16:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default applies to existing rows. After backfill we leave it
    # at 5 so future inserts can rely on the application-level default if
    # they don't supply one explicitly.
    op.add_column(
        "deployment_targets",
        sa.Column(
            "max_retries",
            sa.Integer(),
            nullable=False,
            server_default="5",
        ),
    )


def downgrade() -> None:
    op.drop_column("deployment_targets", "max_retries")
