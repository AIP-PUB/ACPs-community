"""E2E Message 模块 conftest — MessageWriter 后台任务与 CH/Redis 清理。

架构：
- `e2e_message_writer` fixture（function scope）：独立 consumer group 的 MessageWriter
  + LifecycleCompactor / ThroughputCompactor，测试结束后停止并清理状态。
- `e2e_http_client` fixture：绑定到 ASGI app 的 HTTP 客户端（不依赖 Kafka）。
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from tests.support.redis_helper import reset_message_redis_state


@pytest.fixture(scope="session")
def _require_clickhouse_e2e() -> None:
    """防护：CH 连通性检查（跳过而非报错，与 Access E2E 同模式）。"""
    import asyncio

    from app.core.clickhouse_client import get_clickhouse_client

    async def _check() -> None:
        try:
            client = await get_clickhouse_client()
            await client.command("SELECT 1")
        except Exception as exc:
            pytest.skip(f"ClickHouse 不可达，跳过 Message E2E 测试: {exc}")

    asyncio.get_event_loop().run_until_complete(_check())


@pytest.fixture
async def e2e_message_writer(
    _require_clickhouse_e2e: None,
) -> AsyncGenerator[dict[str, Any]]:
    """启动独立 consumer group 的 MessageWriter + Compactors，yield 控制对象。

    Setup 顺序（对齐 tests/e2e/conftest.py e2e_writer 模式）：
    1. reset_message_redis_state() — 清理水位/去重键
    2. ensure_message_schema() — DDL bootstrap（幂等）
    3. writer._group_id = unique — 独立 consumer group，避免污染开发环境
    4. writer._auto_offset_reset = "latest" — 只消费 start() 之后的新消息
    5. await writer.start() — 连接 Kafka，订阅主题
    6. asyncio.create_task(writer.run()) — 启动后台消费循环
    7. asyncio.sleep(1.5) — 等待 consumer join + seek-to-latest

    Teardown：signal stop → stop() → cancel task。
    """
    from app.core.redis_client import get_redis
    from app.message.store import ensure_message_schema

    redis = get_redis()

    await reset_message_redis_state(redis)
    await ensure_message_schema()

    from app.message.lifecycle_compactor import LifecycleCompactor
    from app.message.throughput_compactor import ThroughputCompactor
    from app.message.writer import MessageWriter

    group_id = f"amp.message.writer.e2e.{uuid.uuid4()}"
    writer = MessageWriter(redis)
    writer._group_id = group_id  # 独立 consumer group
    writer._auto_offset_reset = "latest"  # 只消费 start() 之后的新消息

    await writer.start()

    lifecycle_compactor = LifecycleCompactor(redis)
    throughput_compactor = ThroughputCompactor(redis)

    writer_task = asyncio.create_task(writer.run(), name="e2e-message-writer")
    lc_task = asyncio.create_task(lifecycle_compactor.run(), name="e2e-lifecycle-compactor")
    tc_task = asyncio.create_task(throughput_compactor.run(), name="e2e-throughput-compactor")

    await asyncio.sleep(1.5)  # 等 consumer join + seek-to-latest

    try:
        yield {
            "writer": writer,
            "lifecycle_compactor": lifecycle_compactor,
            "throughput_compactor": throughput_compactor,
            "redis": redis,
            "group_id": group_id,
        }
    finally:
        writer._running = False
        await writer.stop()
        for task in [writer_task, lc_task, tc_task]:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await reset_message_redis_state(redis)


@pytest.fixture
async def e2e_http_client() -> AsyncGenerator[AsyncClient]:
    """绑定到 ASGI app 的 HTTP 客户端（纯查询，无需 Kafka）。"""
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
