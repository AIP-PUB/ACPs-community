"""add agent_alive_status and alive_sync_shard_state tables

Revision ID: a1b2c3d4e5f6
Revises: bf6b6ea78c3c
Create Date: 2026-06-14 08:00:00.000000

"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "bf6b6ea78c3c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建 AMP alive-sync 两张存储表。"""
    op.create_table(
        "agent_alive_status",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("aic", sa.String(128), nullable=False, unique=True),
        sa.Column("alive", sa.Boolean, nullable=False, default=False),
        sa.Column("last_seen_at", sa.String(64), nullable=True),
        sa.Column("version", sa.BigInteger, nullable=False, default=0),
        sa.Column("shard", sa.String(32), nullable=False),
    )
    op.create_index(
        "ix_agent_alive_status_aic",
        "agent_alive_status",
        ["aic"],
        unique=True,
    )
    op.create_index(
        "idx_agent_alive_status_shard",
        "agent_alive_status",
        ["shard"],
        unique=False,
    )

    op.create_table(
        "alive_sync_shard_state",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("shard", sa.String(32), nullable=False, unique=True),
        sa.Column("last_seen_seq", sa.BigInteger, nullable=False, default=0),
        sa.Column("cutover_seq", sa.BigInteger, nullable=False, default=0),
        sa.Column("kafka_next_offset", sa.BigInteger, nullable=True),
        sa.Column("snapshot_generated_at", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_alive_sync_shard_state_shard",
        "alive_sync_shard_state",
        ["shard"],
        unique=True,
    )


def downgrade() -> None:
    """删除 AMP alive-sync 两张存储表。"""
    op.drop_index("ix_alive_sync_shard_state_shard", table_name="alive_sync_shard_state")
    op.drop_table("alive_sync_shard_state")
    op.drop_index("idx_agent_alive_status_shard", table_name="agent_alive_status")
    op.drop_index("ix_agent_alive_status_aic", table_name="agent_alive_status")
    op.drop_table("agent_alive_status")
