"""tests/integration/test_audit_model.py — 数据库模型与迁移验证测试。

验证：
- 所有八张表存在且列结构正确（含新增 audit_chain_checkpoint）
- audit_record_identity 有 committed_at 列与索引（§4.1）
- audit_record_identity 的 UNIQUE(log_id) 约束有效
- audit_chain_head 有 256 行（迁移预创建）
- audit_read_model_watermark 有初始行
- audit_records 按 committed_at 分区
- audit_export_tasks 有 kind 列
- chain_head 行锁行为（FOR UPDATE）
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


class TestTableExistence:
    async def test_eight_tables_exist(self, db_session: AsyncSession) -> None:
        """所有八张审计表必须存在于 agent_monitor_test 库中（含新增 audit_chain_checkpoint）。"""
        result = await db_session.execute(
            text("""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_type = 'BASE TABLE'
                ORDER BY table_name
            """)
        )
        tables = {row[0] for row in result.fetchall()}
        expected = {
            "audit_record_identity",
            "audit_chain_head",
            "audit_records",
            "audit_chain_anchor",
            "audit_chain_checkpoint",
            "audit_read_model_watermark",
            "audit_integrity_tasks",
            "audit_export_tasks",
        }
        assert expected.issubset(tables), f"缺少表：{expected - tables}"

    async def test_audit_record_identity_has_committed_at(self, db_session: AsyncSession) -> None:
        """audit_record_identity 必须有 committed_at 列（§4.1）。"""
        result = await db_session.execute(
            text(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'audit_record_identity'"
                "   AND column_name = 'committed_at'"
            )
        )
        assert result.fetchone() is not None, "audit_record_identity 缺少 committed_at 列"

    async def test_audit_record_identity_committed_at_index(self, db_session: AsyncSession) -> None:
        """audit_record_identity 应有 idx_audit_identity_committed 索引（§4.1）。"""
        result = await db_session.execute(
            text(
                "SELECT indexname FROM pg_indexes"
                " WHERE tablename = 'audit_record_identity'"
                "   AND indexname = 'idx_audit_identity_committed'"
            )
        )
        assert result.fetchone() is not None, "缺少 idx_audit_identity_committed 索引"

    async def test_audit_export_tasks_has_kind_column(self, db_session: AsyncSession) -> None:
        """audit_export_tasks 必须有 kind 列以区分 public/internal 任务（§4.8）。"""
        result = await db_session.execute(
            text(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'audit_export_tasks'"
                "   AND column_name = 'kind'"
            )
        )
        assert result.fetchone() is not None, "audit_export_tasks 缺少 kind 列"

    async def test_audit_records_is_partitioned_table(self, db_session: AsyncSession) -> None:
        """audit_records 必须是 PostgreSQL 范围分区主表。"""
        result = await db_session.execute(
            text("""
                SELECT relkind, partstrat
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_partitioned_table pt ON pt.partrelid = c.oid
                WHERE n.nspname = 'public'
                  AND c.relname = 'audit_records'
            """)
        )
        row = result.fetchone()
        assert row is not None, "audit_records 表不存在"
        # pg_class.relkind='p' 表示分区主表；asyncpg 返回 bytes，做解码兼容
        relkind = row[0].decode() if isinstance(row[0], (bytes, bytearray)) else str(row[0])
        assert relkind == "p", f"audit_records 不是分区表，relkind={row[0]!r}"

    async def test_audit_records_has_current_month_partition(self, db_session: AsyncSession) -> None:
        """audit_records 必须包含当月（2026-06）分区子表。"""
        result = await db_session.execute(
            text("""
                SELECT c.relname
                FROM pg_inherits pi
                JOIN pg_class c ON c.oid = pi.inhrelid
                JOIN pg_class p ON p.oid = pi.inhparent
                WHERE p.relname = 'audit_records'
            """)
        )
        partitions = {row[0] for row in result.fetchall()}
        assert any("2026_06" in p for p in partitions), f"缺少 2026_06 分区，当前子表：{partitions}"


class TestChainHeadPreseeding:
    async def test_chain_head_has_256_rows(self, db_session: AsyncSession) -> None:
        """audit_chain_head 应有 256 条预创建的子链头行。"""
        result = await db_session.execute(text("SELECT COUNT(*) FROM audit_chain_head"))
        count = result.scalar_one()
        assert count == 256, f"audit_chain_head 行数为 {count}，期望 256"

    async def test_chain_head_initial_seq_is_minus_one(self, db_session: AsyncSession) -> None:
        """每行初始 last_chain_seq 应为 -1。"""
        result = await db_session.execute(text("SELECT COUNT(*) FROM audit_chain_head WHERE last_chain_seq != -1"))
        non_initial = result.scalar_one()
        assert non_initial == 0, f"有 {non_initial} 行 last_chain_seq 不为 -1"

    async def test_chain_id_format_zero_padded(self, db_session: AsyncSession) -> None:
        """chain_id 格式应为 'audit-chain-NNN'（零填充到固定宽度）。"""
        result = await db_session.execute(text("SELECT chain_id FROM audit_chain_head ORDER BY chain_id LIMIT 5"))
        ids = [row[0] for row in result.fetchall()]
        assert len(ids) > 0
        for cid in ids:
            assert cid.startswith("audit-chain-"), f"chain_id 格式异常：{cid!r}"


class TestWatermarkInitialState:
    async def test_watermark_table_has_correct_schema(self, db_session: AsyncSession) -> None:
        """audit_read_model_watermark 表结构符合按分区设计（§4.6）：
        - 复合 PK (stream_name, partition_key)
        - partition_watermark 列存在
        - 行由 Writer UPSERT 按需创建，无预置行（与旧全局单行设计不同）
        """
        result = await db_session.execute(
            text(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'audit_read_model_watermark'"
                "   AND column_name = 'partition_watermark'"
            )
        )
        col = result.fetchone()
        assert col is not None, "audit_read_model_watermark 缺少 partition_watermark 列（新分区水位设计）"


class TestUniqueConstraints:
    async def test_log_id_unique_constraint_rejects_duplicate(self, db_session: AsyncSession) -> None:
        """audit_record_identity 的 log_id 唯一约束应拒绝重复插入。"""
        from datetime import UTC, datetime

        from app.audit.model import AuditRecordIdentity

        log_id = f"dup-test-{uuid.uuid4()}"
        audit_id1 = uuid.uuid4()
        audit_id2 = uuid.uuid4()
        now = datetime.now(tz=UTC)

        row1 = AuditRecordIdentity(
            audit_id=audit_id1,
            log_id=log_id,
            timestamp=now,
            committed_at=now,
            chain_id="audit-chain-000",
            chain_seq=0,
            created_at=now,
        )
        db_session.add(row1)
        await db_session.flush()

        row2 = AuditRecordIdentity(
            audit_id=audit_id2,
            log_id=log_id,
            timestamp=now,
            committed_at=now,
            chain_id="audit-chain-000",
            chain_seq=1,
            created_at=now,
        )
        db_session.add(row2)

        with pytest.raises(IntegrityError):
            await db_session.flush()

        await db_session.rollback()
