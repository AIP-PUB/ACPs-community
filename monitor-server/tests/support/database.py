"""测试数据库重置工具。

提供 reset_database_state()，在每个集成测试前后通过直接引擎连接清空
可变审计表、重置链头和 watermark，与任何已打开的 AsyncSession 解耦，
避免 asyncpg「another operation is in progress」错误。
"""

from __future__ import annotations

from sqlalchemy import text


async def reset_database_state() -> None:
    """清空测试数据库中的可变审计表，并将链头、watermark 复位到初始状态。

    使用 async_engine.begin() 获取独立连接，避免与测试中的 AsyncSession 共享
    连接状态，确保清理可在任何 fixture 拆除时机安全运行。
    """
    from app.core.db_session import get_async_engine

    engine = get_async_engine()
    # dispose() 关闭已有连接池；后续 begin() 建立全新连接
    await engine.dispose()

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE audit_record_identity, audit_records, audit_chain_anchor,"
                " audit_integrity_tasks, audit_export_tasks RESTART IDENTITY CASCADE"
            )
        )
        await conn.execute(
            text("""
                UPDATE audit_chain_head
                SET last_audit_id    = NULL,
                    last_chain_seq   = -1,
                    last_current_hash = NULL,
                    updated_at       = NOW()
            """)
        )
        # 清空所有分区行，让每个测试从无水位行的干净状态开始。
        # 集成测试只驱动 partition_key=0（默认），UPSERT 会按需创建行；
        # 全局水位 = MIN(partition_watermark) 在只有一行时即等于该行的值。
        await conn.execute(text("DELETE FROM audit_read_model_watermark WHERE stream_name = 'amp.audit'"))
