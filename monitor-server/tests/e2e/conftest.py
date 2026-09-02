"""E2E 测试 conftest — Audit Writer 后台任务与测试环境管理。

架构：
- `e2e_writer` fixture（function scope）：每个测试函数使用独立 consumer group 的 AuditWriter，
  在后台 asyncio task 中运行，测试结束后停止并清理 DB 数据。
- `e2e_http_client` fixture：绑定到 ASGI app 的 HTTP 客户端（不依赖 Kafka）。

为何使用 function scope writer：
- 每个测试使用唯一 consumer group（`amp.audit.writer.e2e.{uuid}`），避免消费到前一个测试遗留的消息。
- 配合 `auto_offset_reset="latest"`，Writer 只处理 start() 后投递的消息。

关键时序：
1. 每个测试前，e2e_writer setup 先调用 reset_database_state() 重置 DB（watermark 回归 epoch），
   防止上次 session 被强杀后遗留的高水位导致 wait_for_watermark_advance 误判。
2. run_task 创建后，asyncio.sleep(1.0) 让消费者完成初次 getmany() → group join →
   seek-to-latest，确保测试投递的消息不会因竞态被跳过。
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.constants import DEFAULT_TEST_DATABASE_DSN
from tests.support.database import reset_database_state
from tests.support.redis_helper import reset_access_redis_state, reset_heartbeat_redis_state, reset_metrics_redis_state


@pytest.fixture(scope="session", autouse=True)
async def _close_shared_clients_on_exit() -> AsyncGenerator[None]:
    """在 E2E session 结束时关闭进程级共享客户端，避免 event loop 关闭后残留连接告警。"""
    yield

    from app.core.clickhouse_client import close_clickhouse_client
    from app.core.opensearch_client import close_opensearch_client
    from app.core.redis_client import close_redis
    from app.metrics.tsdb import close_tsdb_client

    await close_clickhouse_client()
    await close_opensearch_client()
    await close_tsdb_client()
    await close_redis()


@pytest.fixture(scope="session")
def _require_test_db_e2e() -> None:
    """防护：确保 DATABASE_URL 指向测试数据库。"""
    import os

    url = os.environ.get("DATABASE_URL", DEFAULT_TEST_DATABASE_DSN)
    assert "agent_monitor_test" in url, f"E2E 测试只能使用 agent_monitor_test 库，当前 DATABASE_URL={url!r}"


@pytest.fixture(scope="session", autouse=True)
async def _initial_e2e_db_reset(_require_test_db_e2e: None) -> None:
    """E2E session 启动时重置一次数据库（清除上次 session 被强杀后的遗留高水位）。"""
    await reset_database_state()


@pytest.fixture
async def db_session_e2e() -> AsyncGenerator[AsyncSession]:
    """E2E 测试用数据库 session（不回滚，直接提交）。"""
    from app.core.db_session import async_session_factory

    async with async_session_factory() as session:
        yield session


@pytest.fixture
async def e2e_http_client() -> AsyncGenerator[AsyncClient]:
    """HTTP 客户端：优先使用 Justfile 启动的真实测试服务器，fallback 到 in-process ASGI。

    当 TEST_E2E_BASE_URL 设置时（just test e2e），使用真实 HTTP 请求；
    否则（本地直接运行 pytest）使用 ASGITransport（测试模式，跳过 Kafka Consumer）。
    """
    import os

    base_url = os.environ.get("TEST_E2E_BASE_URL", "").strip()
    if base_url:
        async with AsyncClient(base_url=base_url) as client:
            yield client
    else:
        from app.main import app

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client


@pytest.fixture
async def e2e_writer() -> AsyncGenerator[tuple[Any, Ed25519PrivateKey, str]]:
    """启动 AuditWriter 后台任务，使用唯一 consumer group 和测试 MockKeyResolver。

    Setup 顺序：
    1. reset_database_state() — 重置 watermark 为 epoch，清空历史记录
    2. writer.start() — 连接 Kafka，订阅主题
    3. asyncio.create_task(writer.run()) — 启动后台消费循环
    4. asyncio.sleep(1.0) — 等待消费者完成 group join + seek-to-latest

    步骤 4 确保消费者已就位再将控制权交给测试，避免测试投递消息时消费者尚未
    完成初次 getmany() seek 导致消息被跳过。

    Yields:
        (writer, private_key, kid) — 供测试用于生成合法签名的事件。
    """
    from app.audit.key_resolver import MockKeyResolver
    from app.audit.writer import AuditWriter

    # 每个测试开始前重置 DB，防止跨测试水位污染
    await reset_database_state()

    priv = Ed25519PrivateKey.generate()
    kid = f"e2e-kid-{uuid.uuid4()}"
    pub_pem = priv.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    resolver = MockKeyResolver({kid: pub_pem})

    writer = AuditWriter(key_resolver=resolver)
    # 使用唯一 consumer group，避免污染开发环境 group 及消费到历史消息
    writer._group_id = f"amp.audit.writer.e2e.{uuid.uuid4()}"
    # latest：只消费 start() 之后投递的新消息
    writer._auto_offset_reset = "latest"

    await writer.start()
    run_task = asyncio.create_task(writer.run(), name="e2e_audit_writer")

    # 等待消费者完成初次 getmany() → group join → seek-to-latest（本地 Redpanda 通常 < 200ms）
    await asyncio.sleep(1.0)

    yield writer, priv, kid

    # 停止 writer
    writer._running = False
    await writer.stop()
    run_task.cancel()
    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(run_task), timeout=3.0)

    # 为下一个测试清理 DB
    await reset_database_state()


@pytest.fixture
async def e2e_heartbeat_runtime() -> AsyncGenerator[None]:
    """启动 Heartbeat 后台任务（Writer + Reconciler），每个测试函数使用唯一 consumer group。

    Setup 顺序：
    1. reset_heartbeat_redis_state() — 清理 amp:hb:* 键（防止水位/状态污染）
    2. ensure_functions_loaded() — 加载 Redis Functions（幂等）
    3. writer._group_id 覆盖为唯一 e2e 专用 group，避免消费历史消息
    4. writer._auto_offset_reset = "latest"
    5. writer.start() + create_task(writer.run())
    6. create_task(reconciler.run())
    7. asyncio.sleep(1.0) — 等待 writer 完成 group join + seek-to-latest

    Teardown：逆序 cancel tasks + stop writer。
    """
    from app.core.redis_client import get_redis
    from app.heartbeat.functions import ensure_functions_loaded
    from app.heartbeat.reconciler import HeartbeatReconciler
    from app.heartbeat.snapshot import get_snapshot_exporter
    from app.heartbeat.writer import HeartbeatWriter

    redis = get_redis()
    await reset_heartbeat_redis_state(redis)
    await ensure_functions_loaded(redis)

    # 重置 SnapshotExporter 进程级缓存，防止跨测试函数的快照数据污染
    get_snapshot_exporter()._cached = None

    writer = HeartbeatWriter(redis)
    writer._group_id = f"amp.heartbeat.writer.e2e.{uuid.uuid4()}"
    writer._auto_offset_reset = "latest"

    reconciler = HeartbeatReconciler(redis)

    await writer.start()
    writer_task = asyncio.create_task(writer.run(), name="e2e_heartbeat_writer")
    reconciler_task = asyncio.create_task(reconciler.run(), name="e2e_heartbeat_reconciler")

    # 等待 writer 完成 group join + seek-to-latest
    await asyncio.sleep(1.0)

    yield

    # 停止 writer（flush 水位）
    writer._running = False
    with contextlib.suppress(Exception):
        await writer._flush_watermarks()
    await writer.stop()

    # Cancel tasks
    for task in (reconciler_task, writer_task):
        task.cancel()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=3.0)


@pytest.fixture
async def e2e_metrics_runtime() -> AsyncGenerator[None]:
    """启动 MetricsWriter 后台任务（E2E），每个测试使用唯一 consumer group。

    Setup 顺序：
    1. reset_metrics_redis_state() — 清理 amp:metrics:* 键
    2. writer._group_id 覆盖为唯一 e2e 专用 group，避免消费历史消息
    3. writer._auto_offset_reset = "latest"
    4. writer.start() + create_task(writer.run())
    5. asyncio.sleep(1.0) — 等待 writer 完成 group join + seek-to-latest

    Yields:
        None — 测试通过 kafka_helper.produce_metrics 投递消息，轮询 snapshots/query 验证。
    """
    from app.core.redis_client import get_redis
    from app.metrics.tsdb import close_tsdb_client
    from app.metrics.writer import MetricsWriter

    redis = get_redis()
    await reset_metrics_redis_state(redis)
    # 注入近期水位，避免 Writer 首次 flush 前 snapshots/query 返回 503（水位未知）
    import time

    from tests.support.redis_helper import seed_watermark

    await seed_watermark(redis, int(time.time() * 1000))
    await close_tsdb_client()

    writer = MetricsWriter(redis)
    writer._group_id = f"amp.metrics.writer.e2e.{uuid.uuid4()}"
    writer._auto_offset_reset = "latest"

    await writer.start()
    run_task = asyncio.create_task(writer.run(), name="e2e_metrics_writer")

    # 等待 writer 完成 group join + seek-to-latest
    await asyncio.sleep(1.0)

    yield

    # 停止 writer
    writer._running = False
    await writer.stop()
    run_task.cancel()
    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(run_task), timeout=3.0)

    await close_tsdb_client()
    await reset_metrics_redis_state(redis)


@pytest.fixture
async def e2e_access_runtime() -> AsyncGenerator[None]:
    """启动 AccessWriter 后台任务（E2E），每个测试使用唯一 consumer group。

    Setup 顺序：
    1. reset_access_redis_state() — 清理 amp:access:* 键（防止水位/去重标记污染）
    2. ensure_test_schema() — DDL bootstrap（建三表两视图，幂等）
    3. truncate_access_tables() — 清空数据，确保干净状态
    4. writer._group_id 覆盖为唯一 e2e 专用 group，避免消费历史消息
    5. writer._auto_offset_reset = "latest"
    6. writer.start() + create_task(writer.run())
    7. asyncio.sleep(1.0) — 等待 writer 完成 group join + seek-to-latest

    Teardown：停止 writer + truncate + reset redis。
    """
    from app.access.writer import AccessWriter
    from app.core.redis_client import get_redis
    from tests.support.clickhouse_helper import ensure_test_schema, truncate_access_tables

    redis = get_redis()
    await reset_access_redis_state(redis)
    await ensure_test_schema()
    await truncate_access_tables()

    writer = AccessWriter(redis)
    writer._group_id = f"amp.access.writer.e2e.{uuid.uuid4()}"
    writer._auto_offset_reset = "latest"

    await writer.start()
    run_task = asyncio.create_task(writer.run(), name="e2e_access_writer")

    # 等待 writer 完成 group join + seek-to-latest
    await asyncio.sleep(1.0)

    yield

    # 停止 writer
    writer._running = False
    await writer.stop()
    run_task.cancel()
    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(run_task), timeout=3.0)

    await truncate_access_tables()
    await reset_access_redis_state(redis)
