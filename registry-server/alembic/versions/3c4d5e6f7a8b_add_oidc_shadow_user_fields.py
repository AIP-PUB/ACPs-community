"""add oidc shadow user fields

Revision ID: 3c4d5e6f7a8b
Revises: e24d8c3b7f11
Create Date: 2026-06-24 18:40:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "3c4d5e6f7a8b"
down_revision: str | None = "e24d8c3b7f11"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "account_user",
        sa.Column("auth_provider", sa.String(length=32), nullable=False, server_default="local"),
    )
    op.add_column("account_user", sa.Column("external_issuer", sa.String(), nullable=True))
    op.add_column("account_user", sa.Column("external_subject", sa.String(), nullable=True))
    op.add_column("account_user", sa.Column("external_principal_id", sa.String(), nullable=True))
    op.add_column("account_user", sa.Column("external_username", sa.String(), nullable=True))
    op.add_column("account_user", sa.Column("last_login_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.create_index(op.f("ix_account_user_auth_provider"), "account_user", ["auth_provider"], unique=False)
    op.create_index(op.f("ix_account_user_external_issuer"), "account_user", ["external_issuer"], unique=False)
    op.create_index(op.f("ix_account_user_external_subject"), "account_user", ["external_subject"], unique=False)
    op.create_index(
        op.f("ix_account_user_external_principal_id"),
        "account_user",
        ["external_principal_id"],
        unique=True,
    )
    op.create_unique_constraint(
        "uq_account_user_external_issuer_subject",
        "account_user",
        ["external_issuer", "external_subject"],
    )
    op.alter_column("account_user", "auth_provider", server_default=None)


def downgrade() -> None:
    op.drop_constraint("uq_account_user_external_issuer_subject", "account_user", type_="unique")
    op.drop_index(op.f("ix_account_user_external_principal_id"), table_name="account_user")
    op.drop_index(op.f("ix_account_user_external_subject"), table_name="account_user")
    op.drop_index(op.f("ix_account_user_external_issuer"), table_name="account_user")
    op.drop_index(op.f("ix_account_user_auth_provider"), table_name="account_user")
    op.drop_column("account_user", "last_login_at")
    op.drop_column("account_user", "external_username")
    op.drop_column("account_user", "external_principal_id")
    op.drop_column("account_user", "external_subject")
    op.drop_column("account_user", "external_issuer")
    op.drop_column("account_user", "auth_provider")
