"""add account user aic_provider_code

Revision ID: 6a7b8c9d0e1f
Revises: 4e1f2a3b4c5d
Create Date: 2026-08-27 18:40:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "6a7b8c9d0e1f"
down_revision: str | None = "4e1f2a3b4c5d"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column("account_user", sa.Column("aic_provider_code", sa.String(length=6), nullable=True))
    op.create_index(
        "uq_account_user_aic_provider_code",
        "account_user",
        ["aic_provider_code"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_account_user_aic_provider_code", table_name="account_user")
    op.drop_column("account_user", "aic_provider_code")
