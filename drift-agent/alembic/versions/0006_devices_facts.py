"""devices.facts

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-20 19:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Identity facts (interfaces, hostname, arch, os, kernel,
    # docker_version) reported by the agent every ~10min. Nullable so
    # devices commissioned pre-v0.5.3 don't break — they'll populate
    # on their next check-in once the new agent reports facts.
    op.add_column(
        "devices",
        sa.Column("facts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("devices", "facts")
