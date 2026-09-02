"""E2E System 模块 conftest — SystemWriter 后台任务与 OpenSearch/Redis 清理。

架构：
- `e2e_system_runtime` fixture（function scope）：独立 consumer group 的 SystemWriter，
  测试结束后停止并清理 Redis 水位。
- `e2e_http_client` fixture：绑定到 ASGI app 的 HTTP 客户端（不依赖 Kafka）。

关键时序（与 message conftest 相同模式）：
1. reset_system_redis_state() 清理 amp:system:wm:* 水位键
2. ensure_system_schema() 确保 OpenSearch 索引模板 + ISM 策略（幂等）
3. writer._group_id 覆盖为唯一 e2e 专用 group，避免消费历史消息
4. writer._auto_offset_reset = "latest" — 只消费 start() 之后的新消息
5. writer.start() + create_task(writer.run())
6. asyncio.sleep(1.5) — 等待 consumer join + seek-to-latest
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from tests.support.opensearch_helper import delete_indices
from tests.support.redis_helper import reset_system_redis_state


@pytest.fixture(autouse=True)
async def cleanup_system_indices(_require_opensearch_e2e: None) -> AsyncGenerator[None]:
    """每测试前后删除 amp-system-events-* 索引，防止状态污染（H-1）。"""
    await delete_indices()
    yield
    await delete_indices()


@pytest.fixture(scope="session")
async def _require_opensearch_e2e() -> AsyncGenerator[None]:
    """防护：OpenSearch 连通性检查（不可达则失败，提示先执行 bootstrap）。"""
    from app.core.config import settings
    from app.core.opensearch_client import check_opensearch, close_opensearch_client
    from app.system import store

    ok = await check_opensearch()
    if not ok:
        pytest.fail("OpenSearch 不可达，请先执行 just test bootstrap 或 just infra up opensearch")

    await store.ensure_system_schema(
        number_of_shards=settings.system_index_number_of_shards,
        number_of_replicas=settings.system_index_number_of_replicas,
        hot_days=settings.system_event_hot_retention_days,
        warm_days=settings.system_event_warm_retention_days,
        archive_days=settings.system_archive_retention_days,
    )

    yield

    await close_opensearch_client()


@pytest.fixture
async def e2e_system_runtime(
    _require_opensearch_e2e: None,
) -> AsyncGenerator[dict[str, Any]]:
    """启动独立 consumer group 的 SystemWriter，yield 控制对象。

    Setup 顺序：
    1. reset_system_redis_state() — 清理 amp:system:wm:* 键
    2. ensure_system_schema() — OpenSearch 索引模板 + ISM 策略（幂等）
    3. writer._group_id = unique — 独立 consumer group
    4. writer._auto_offset_reset = "latest" — 只消费 start() 之后的新消息
    5. await writer.start() — 连接 Kafka，订阅主题
    6. asyncio.create_task(writer.run()) — 启动后台消费循环
    7. asyncio.sleep(1.5) — 等待 consumer join + seek-to-latest

    Teardown：stop writer → cancel task → reset redis watermarks。
    """
    from app.core.redis_client import get_redis
    from app.system.writer import SystemWriter

    redis = get_redis()
    await reset_system_redis_state(redis)

    group_id = f"amp.system.writer.e2e.{uuid.uuid4()}"
    writer = SystemWriter(redis)
    writer._group_id = group_id
    writer._auto_offset_reset = "latest"

    await writer.start()
    writer_task = asyncio.create_task(writer.run(), name="e2e-system-writer")

    await asyncio.sleep(1.5)

    try:
        yield {
            "writer": writer,
            "redis": redis,
            "group_id": group_id,
        }
    finally:
        await writer.stop()
        writer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await writer_task
        await reset_system_redis_state(redis)


@pytest.fixture
async def opensearch_client(_require_opensearch_e2e: None) -> AsyncGenerator[Any]:
    """OpenSearch 异步客户端（E2E 直接断言索引用）。"""
    from app.core.opensearch_client import get_opensearch_client

    yield await get_opensearch_client()


@pytest.fixture
async def redis_client(_require_opensearch_e2e: None) -> AsyncGenerator[Any]:
    """Redis 异步客户端（E2E 水位/去重键断言用）。"""
    from app.core.redis_client import get_redis

    yield get_redis()


@pytest.fixture
async def e2e_http_client() -> AsyncGenerator[AsyncClient]:
    """绑定到 ASGI app 的 HTTP 客户端（纯查询，无需 Kafka）。"""
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
