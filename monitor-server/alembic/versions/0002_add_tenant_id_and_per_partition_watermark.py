"""add tenant_id and per-partition watermark

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-11

变更内容：
1. audit_records 加 tenant_id 列及其索引（§4.3）
2. audit_read_model_watermark 重建为按分区跟踪（§4.6, C-AUDIT-WRITE-3, C-AUDIT-QUERY-7）
   - 旧表：stream_name PRIMARY KEY + event_time_watermark（全局单行）
   - 新表：(stream_name, partition_key) 复合 PK + partition_watermark + last_offset
   - Writer 以 UPSERT 按需创建分区行；全局水位 = MIN(partition_watermark)
"""

from __future__ import annotations

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    # ── 1. audit_records 加 tenant_id 列 ─────────────────────────────────
    op.execute("ALTER TABLE audit_records ADD COLUMN IF NOT EXISTS tenant_id TEXT")
    op.execute("CREATE INDEX IF NOT EXISTS idx_audit_tenant_ts ON audit_records (tenant_id, timestamp DESC)")

    # ── 2. 重建 audit_read_model_watermark 为按分区 schema ────────────────
    # 备注：旧表仅有一行全局水位，新表改为 (stream_name, partition_key) 复合 PK。
    # 旧数据迁移：把旧全局水位转换为 partition_key=0 的初始行，保留已有时间信息。
    op.execute("""
        CREATE TABLE audit_read_model_watermark_new (
            stream_name         TEXT        NOT NULL,
            partition_key       BIGINT      NOT NULL,
            partition_watermark TIMESTAMPTZ NOT NULL,
            last_offset         BIGINT,
            updated_at          TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (stream_name, partition_key)
        )
    """)

    # 迁移旧数据：把原全局水位插入 partition_key=0 行
    op.execute("""
        INSERT INTO audit_read_model_watermark_new
            (stream_name, partition_key, partition_watermark, last_offset, updated_at)
        SELECT stream_name, 0, event_time_watermark, NULL, updated_at
        FROM audit_read_model_watermark
        ON CONFLICT DO NOTHING
    """)

    op.execute("DROP TABLE audit_read_model_watermark")
    op.execute("ALTER TABLE audit_read_model_watermark_new RENAME TO audit_read_model_watermark")


def downgrade() -> None:
    # ── 1. 回退 audit_read_model_watermark ────────────────────────────────
    op.execute("""
        CREATE TABLE audit_read_model_watermark_old (
            stream_name             TEXT        PRIMARY KEY,
            event_time_watermark    TIMESTAMPTZ NOT NULL,
            updated_at              TIMESTAMPTZ NOT NULL,
            source_offsets          JSONB
        )
    """)
    # 取各 stream 的最小水位作为回退后的全局水位
    op.execute("""
        INSERT INTO audit_read_model_watermark_old
            (stream_name, event_time_watermark, updated_at, source_offsets)
        SELECT stream_name, MIN(partition_watermark), MAX(updated_at), NULL
        FROM audit_read_model_watermark
        GROUP BY stream_name
        ON CONFLICT DO NOTHING
    """)
    op.execute("DROP TABLE audit_read_model_watermark")
    op.execute("ALTER TABLE audit_read_model_watermark_old RENAME TO audit_read_model_watermark")

    # ── 2. 回退 audit_records ─────────────────────────────────────────────
    op.execute("DROP INDEX IF EXISTS idx_audit_tenant_ts")
    op.execute("ALTER TABLE audit_records DROP COLUMN IF EXISTS tenant_id")
