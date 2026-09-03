"""app/core/clickhouse_client.py — 异步 ClickHouse 客户端工厂。

模式对齐 redis_client.py：模块级懒初始化单例 + 显式 close + 探活 check。
仅提供连接/执行/批量 insert/探活原语；各日志类型的表 DDL 与 SQL 留在各自模块。

access 事件真相源；未来 message 读模型复用。
"""

from __future__ import annotations

from typing import Any

import clickhouse_connect
import structlog

from app.core.config import settings

logger = structlog.get_logger(__name__)

_client: Any = None


async def get_clickhouse_client() -> Any:
    """获取进程级 ClickHouse 异步单例（懒初始化）。

    clickhouse_connect async client 内部以线程池包裹同步 client，对事件循环非阻塞。
    connect_timeout 取自 access_query_timeout_seconds；
    send_receive_timeout 略大（+ 15s 缓冲），防止客户端超时早于服务端返回响应。
    """
    global _client
    if _client is None:
        query_timeout = settings.access_query_timeout_seconds
        # send_receive_timeout 必须大于 max_execution_time：
        # ClickHouse 服务端超时后需要时间序列化并发回错误响应；
        # 若客户端先超时，会丢失 ClickHouse 的错误信息并以 SocketTimeoutError 替代。
        socket_timeout = max(60, query_timeout + 15)
        _client = await clickhouse_connect.get_async_client(
            host=settings.clickhouse_host,
            port=settings.clickhouse_port,
            username=settings.clickhouse_user,
            password=settings.clickhouse_password,
            database=settings.clickhouse_database,
            query_limit=0,
            connect_timeout=query_timeout,
            send_receive_timeout=socket_timeout,
        )
    return _client


async def close_clickhouse_client() -> None:
    """关闭 ClickHouse 客户端（lifespan 关闭时调用；幂等）。"""
    global _client
    if _client is not None:
        await _client.close()
        _client = None


async def check_clickhouse() -> bool:
    """探活：SELECT 1；使用独立连接（不依赖业务库是否已创建）。

    health check 只需验证 ClickHouse server 可达，业务库由 DDL bootstrap 负责创建。
    """
    try:
        probe = await clickhouse_connect.get_async_client(
            host=settings.clickhouse_host,
            port=settings.clickhouse_port,
            username=settings.clickhouse_user,
            password=settings.clickhouse_password,
            connect_timeout=5,
            send_receive_timeout=5,
        )
        await probe.query("SELECT 1")
        await probe.close()
        return True
    except Exception:
        logger.exception("ClickHouse health check failed")
        return False
