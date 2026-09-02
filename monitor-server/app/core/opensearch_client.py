"""app/core/opensearch_client.py — 异步 OpenSearch 客户端工厂。

模式对齐 clickhouse_client.py / redis_client.py：模块级懒初始化单例 + 显式 close + 探活 check。
仅提供连接/搜索/Bulk/PIT/索引管理原语；system 专属索引名、模板、ISM、DSL 留在 app/system。

system 是首个 OpenSearch 后端用户，故此原语入 core（与 access 引入 clickhouse_client 入 core 先例同型）。
版本要求：OpenSearch ≥ 2.12（flat_object dotted-path term 查询 bug fix，设计 §4.1）。
"""

from __future__ import annotations

import structlog
from opensearchpy import AsyncOpenSearch

from app.core.config import settings

logger = structlog.get_logger(__name__)

_client: AsyncOpenSearch | None = None


async def get_opensearch_client() -> AsyncOpenSearch:
    """获取进程级 OpenSearch 异步单例（懒初始化）。

    async 确保在 event loop 内执行，aiohttp ClientSession 安全（对齐 clickhouse_client 先例）。
    hosts 读 settings.opensearch_hosts（逗号分隔多节点），连接配置读 opensearch_* 相关配置。
    """
    global _client
    if _client is None:
        hosts_raw = settings.opensearch_hosts
        hosts = [h.strip() for h in hosts_raw.split(",") if h.strip()]
        kwargs: dict[str, object] = {
            "hosts": hosts,
            "use_ssl": hosts_raw.startswith("https"),
            "verify_certs": settings.opensearch_verify_certs,
            "http_compress": True,
            "timeout": settings.system_query_timeout_seconds,
        }
        user = settings.opensearch_user
        password = settings.opensearch_password
        if user:
            kwargs["http_auth"] = (user, password)
        _client = AsyncOpenSearch(**kwargs)  # type: ignore[arg-type]
        logger.info("OpenSearch 客户端初始化完成", hosts=hosts)
    return _client


async def close_opensearch_client() -> None:
    """关闭 OpenSearch 客户端（lifespan 关闭时调用；幂等）。"""
    global _client
    if _client is not None:
        await _client.close()
        _client = None
        logger.info("OpenSearch 客户端已关闭")


async def check_opensearch() -> bool:
    """探活：cluster health 或 ping；使用现有单例；异常 → False（不抛）。

    只验证 OpenSearch server 可达，索引由 bootstrap 创建（store.ensure_system_schema）。
    """
    try:
        client = await get_opensearch_client()
        health = await client.cluster.health()
        status = health.get("status", "unknown")
        return status in {"green", "yellow"}
    except Exception:
        logger.exception("OpenSearch health check failed")
        return False
