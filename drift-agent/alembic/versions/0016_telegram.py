"""telegram

Revision ID: 0016
Revises: 0015
Create Date: 2026-06-26 00:00:00

Two-table addition for the Telegram bot feature:

  telegram_link_codes  short-lived /link codes a user generates in the SPA
                       (or CLI) and types into the bot as `/link <code>`
                       to bind their chat to their account.

  telegram_chats       resolved bindings — one row per linked chat_id.
                       Lookup direction is chat_id → user_id for the bot
                       loop (incoming message → which Drift user does
                       this belong to).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "telegram_link_codes",
        sa.Column("code", sa.String(16), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_telegram_link_codes_user_id", "telegram_link_codes", ["user_id"]
    )

    op.create_table(
        "telegram_chats",
        # chat_id is Telegram's int64 — store as text so we don't have to
        # decide between Integer/BigInteger and to keep parity with the
        # JSON shape Telegram uses elsewhere (where it's serialized as
        # a string in webhook bodies).
        sa.Column("chat_id", sa.String(32), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(128), nullable=True),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_telegram_chats_user_id", "telegram_chats", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_telegram_chats_user_id", table_name="telegram_chats")
    op.drop_table("telegram_chats")
    op.drop_index("ix_telegram_link_codes_user_id", table_name="telegram_link_codes")
    op.drop_table("telegram_link_codes")
