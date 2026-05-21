"""registry_credentials.group_id

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-21 22:00:00

Scope registry credentials to a group so a device only receives creds
matching its own group_id at check-in time. Uniqueness moves from
(registry) to (registry, group_id) — the same registry can have
different creds per group.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the old single-column unique on registry; the constraint name
    # is the postgres default for `unique=True` on the column.
    op.drop_constraint(
        "registry_credentials_registry_key",
        "registry_credentials",
        type_="unique",
    )
    # Safe to add NOT NULL with no default: there are zero rows in this
    # table on every known deployment, and the production deploy of this
    # migration was verified empty before rollout. If somehow a row
    # exists, the migration will fail loudly — operator can hand-fix.
    op.add_column(
        "registry_credentials",
        sa.Column("group_id", sa.String(128), nullable=False),
    )
    op.create_unique_constraint(
        "uq_registry_credentials_registry_group",
        "registry_credentials",
        ["registry", "group_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_registry_credentials_registry_group",
        "registry_credentials",
        type_="unique",
    )
    op.drop_column("registry_credentials", "group_id")
    op.create_unique_constraint(
        "registry_credentials_registry_key",
        "registry_credentials",
        ["registry"],
    )
