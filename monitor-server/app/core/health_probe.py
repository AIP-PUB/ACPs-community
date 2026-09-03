"""健康探针：检查数据库连通性与 ClickHouse 连通性。

/health 端点调用此模块以判断服务是否就绪。
Kafka Consumer 状态通过 metrics 暴露，不在此检测。
"""

from __future__ import annotations

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = structlog.get_logger(__name__)


async def check_database(engine: AsyncEngine) -> bool:
    """向数据库发送 SELECT 1 探针，返回是否连通。"""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.exception("DB health check failed")
        return False


async def check_clickhouse() -> bool:
    """向 ClickHouse 发送 SELECT 1 探针，返回是否连通。"""
    try:
        from app.core.clickhouse_client import check_clickhouse as _ch_check

        return await _ch_check()
    except Exception:
        logger.exception("ClickHouse health check failed")
        return False


async def check_opensearch() -> bool:
    """向 OpenSearch 发送 cluster health 探针，返回是否连通。"""
    try:
        from app.core.opensearch_client import check_opensearch as _os_check

        return await _os_check()
    except Exception:
        logger.exception("OpenSearch health check failed")
        return False
