"""merge oidc and account auth heads

Revision ID: 4e1f2a3b4c5d
Revises: 3c4d5e6f7a8b, f1a2b3c4d5e6
Create Date: 2026-06-25 10:30:00.000000
"""

from __future__ import annotations

revision: str = "4e1f2a3b4c5d"
down_revision: tuple[str, str] = ("3c4d5e6f7a8b", "f1a2b3c4d5e6")
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """合并 OIDC 与账号验证分支的 migration head。"""
    pass


def downgrade() -> None:
    """拆分合并后的 migration head。"""
    pass
