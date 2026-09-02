"""异步 Redis 客户端工厂（heartbeat 真相源；未来 metrics snapshot cache 复用）。

模式对齐 db_session.py：模块级懒初始化单例 + 显式 close + 探活 check。
"""

from __future__ import annotations

import structlog
from redis.asyncio import Redis

from app.core.config import settings

logger = structlog.get_logger(__name__)

_redis: Redis | None = None


def get_redis() -> Redis:
    """获取进程级 Redis 单例（懒初始化）。

    Returns:
        已初始化的 Redis 异步客户端实例（decode_responses=True，所有 key/value 为 str）。
    """
    global _redis
    if _redis is None:
        _redis = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=settings.redis_max_connections,
            socket_timeout=settings.redis_socket_timeout_seconds,
        )
    return _redis


async def close_redis() -> None:
    """关闭 Redis 连接池（lifespan 关闭时调用；幂等）。"""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


async def check_redis() -> bool:
    """发送 PING 探活，返回 Redis 是否可达（供 /health 使用）。

    Returns:
        True 表示 Redis 可达，False 表示连接失败（异常全部吞掉，与 check_database 同风格）。
    """
    try:
        client = get_redis()
        result = await client.ping()
        return bool(result)
    except Exception:
        logger.exception("Redis health check failed")
        return False
