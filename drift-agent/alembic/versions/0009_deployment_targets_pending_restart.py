"""deployment_targets.pending_restart

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-22 19:00:00

One-shot restart signal. Set by the LLM `restart_app_*` tools (and an
admin route); cleared by the CP the moment the next check-in returns
a DesiredApp(action='restart') for the same target.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "deployment_targets",
        sa.Column(
            "pending_restart",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("deployment_targets", "pending_restart")
