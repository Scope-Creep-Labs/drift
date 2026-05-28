"""devices.host_fingerprint + case-insensitive partial unique on name

Two related changes:

1) Add devices.host_fingerprint VARCHAR(64). The edge agent now sends a
   hash of /etc/machine-id (with fallbacks) on every check-in; the CP
   records it on first arrival (TOFU) and rejects mismatches on
   subsequent check-ins. Stops accidental cross-host paste of the
   commissioning curl from silently flipping state between two machines.

2) Replace the existing case-sensitive UNIQUE on devices.name with a
   partial unique index on LOWER(name) WHERE status != 'removed'. So:

   - "pi-001" and "Pi-001" collide at commission time (case-insensitive).
   - A removed device's name is freely reusable (lenient tombstones —
     hard-delete is the current path; the partial filter future-proofs
     for soft-delete).

   The migration normalizes existing device names in place (strip +
   lower). If two existing rows collide after normalization (extremely
   rare; would require devices like "pi-001" and "Pi-001" already in
   the same fleet), the migration fails with a clear error and the
   operator resolves the collision by hand before re-running.

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-28 00:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column("host_fingerprint", sa.String(length=64), nullable=True),
    )

    # Pre-check for case/whitespace collisions before we normalize. If
    # the migration silently picked a winner, the operator would lose a
    # device row. Surface the conflict and let them resolve it.
    bind = op.get_bind()
    collisions = bind.execute(sa.text(
        """
        SELECT LOWER(TRIM(name)) AS norm, COUNT(*) AS n,
               array_agg(name ORDER BY created_at) AS originals
          FROM devices
         GROUP BY LOWER(TRIM(name))
        HAVING COUNT(*) > 1
        """
    )).fetchall()
    if collisions:
        details = "; ".join(
            f"'{row.norm}' ← {row.originals}" for row in collisions
        )
        raise RuntimeError(
            "Migration 0011 refusing to normalize: existing device names "
            "would collide after lower+trim. Resolve in the database "
            "manually (rename or delete the duplicates), then re-run "
            f"alembic upgrade. Collisions: {details}"
        )

    # Normalize existing names in place. Cheap on small fleets; even
    # 10k rows is one UPDATE.
    op.execute("UPDATE devices SET name = LOWER(TRIM(name)) WHERE name <> LOWER(TRIM(name))")

    # Drop the existing case-sensitive UNIQUE constraint. The default
    # name SQLAlchemy gives a unique=True column-level constraint is
    # `<table>_<column>_key` on PostgreSQL.
    op.drop_constraint("devices_name_key", "devices", type_="unique")

    # Partial unique on LOWER(name) — case-insensitive AND lenient
    # toward removed-status rows so a name freed up by delete is
    # reusable. Doubles as the lookup index for `_device_by_name`.
    op.create_index(
        "ix_devices_name_active_unique",
        "devices",
        [sa.text("LOWER(name)")],
        unique=True,
        postgresql_where=sa.text("status <> 'removed'"),
    )


def downgrade() -> None:
    op.drop_index("ix_devices_name_active_unique", table_name="devices")
    op.create_unique_constraint("devices_name_key", "devices", ["name"])
    op.drop_column("devices", "host_fingerprint")
