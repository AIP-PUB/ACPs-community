"""tests/support/clickhouse_helper.py — Access / Message 集成测试 ClickHouse 辅助函数。

提供：
- 建表（ensure_test_schema）
- 清空（truncate_access_tables / truncate_message_tables）
- 批量插入（insert_raw_events / insert_message_events）
- 行工厂（make_access_event_row / make_message_event_row）
- fake_ch_client（单元测试用，无需真实 CH）
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from app.access.events import EventRow
from app.access.tables import (
    ACCESS_EVENTS,
    ACCESS_TOPOLOGY_EDGE_5M,
    ACCESS_TRACE_SPAN,
    INSERT_COLUMNS,
)
from tests.support.constants import TEST_CLICKHOUSE_DATABASE


async def ensure_test_schema() -> None:
    """在测试库中建数据库（IF NOT EXISTS）+ 三表两视图（幂等，CREATE IF NOT EXISTS）。

    ClickHouse 容器只默认创建 `amp` 数据库，`amp_test` 不会自动存在。
    通过 `amp` 数据库临时连接先建库，再用正常客户端单例建表。
    """
    from app.core.config import settings

    # 若测试库与默认库不同，先用默认库连接创建测试库（避免首次 amp_test 不存在时连接失败）
    if settings.clickhouse_database != "amp":
        import clickhouse_connect

        bootstrap_client = await clickhouse_connect.get_async_client(
            host=settings.clickhouse_host,
            port=settings.clickhouse_port,
            username=settings.clickhouse_user,
            password=settings.clickhouse_password,
            database="amp",
            connect_timeout=10,
            send_receive_timeout=10,
        )
        try:
            await bootstrap_client.command(f"CREATE DATABASE IF NOT EXISTS `{settings.clickhouse_database}`")
        finally:
            await bootstrap_client.close()

    from app.access.store import ensure_access_schema
    from app.core.clickhouse_client import close_clickhouse_client
    from app.message.store import ensure_message_schema

    try:
        await ensure_access_schema()
        await ensure_message_schema()
    finally:
        await close_clickhouse_client()


async def truncate_access_tables(client: Any | None = None) -> None:
    """清空 Access 三张主表（集成测试隔离）。"""
    if client is None:
        from app.core.clickhouse_client import get_clickhouse_client

        client = await get_clickhouse_client()
    db = TEST_CLICKHOUSE_DATABASE
    for table in (ACCESS_EVENTS, ACCESS_TRACE_SPAN, ACCESS_TOPOLOGY_EDGE_5M):
        await client.command(f"TRUNCATE TABLE IF EXISTS `{db}`.`{table}`")


async def insert_raw_events(rows: list[EventRow], client: Any | None = None) -> None:
    """直接向 access_events 插入测试行（绕过 Writer/去重）。"""
    if not rows:
        return
    if client is None:
        from app.core.clickhouse_client import get_clickhouse_client

        client = await get_clickhouse_client()
    data = [row.as_tuple() for row in rows]
    db = TEST_CLICKHOUSE_DATABASE
    await client.insert(f"`{db}`.`{ACCESS_EVENTS}`", data, column_names=list(INSERT_COLUMNS))


def make_access_event_row(**overrides: Any) -> EventRow:
    """构造合法 EventRow（可通过 overrides 覆盖任意字段）。

    返回的行可直接传入 insert_raw_events 或 store.insert_events。
    """
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    defaults: dict[str, Any] = {
        "log_id": str(uuid.uuid4()),
        "timestamp_ms": now_ms,
        "observed_at_ms": now_ms,
        "aic": "aic-test-001",
        "trace_id": "",
        "span_id": "",
        "parent_span_id": "",
        "correlation_id": "",
        "severity": "INFO",
        "duration_ms": 50,
        "request_method": "GET",
        "request_route": "/health",
        "request_url": "/health",
        "request_size": 0,
        "request_headers": {},
        "response_status": 200,
        "response_size": 0,
        "response_headers": {},
        "caller_aic": "",
        "caller_service": "",
        "caller_ip": "",
        "callee_aic": "aic-test-001",
        "callee_service": "demo-svc",
        "callee_ip": "",
        "error_code": "",
        "error_message": "",
        "service_name": "demo-svc",
        "deployment_env": "testing",
        "attributes": {},
        "raw_log": "",
    }
    defaults.update(overrides)
    return EventRow(**defaults)


def fake_ch_client() -> tuple[MagicMock, list[tuple]]:
    """返回 (mock_client, captured_inserts) 供单元测试断言 insert 行为。

    captured_inserts 是 insert 调用时 data 参数的累积列表（每次 insert 追加）。

    示例::

        client, captures = fake_ch_client()
        with patch("app.core.clickhouse_client.get_clickhouse_client", AsyncMock(return_value=client)):
            await store.insert_events(rows)
        assert len(captures) == len(rows)
    """
    captured: list[tuple] = []

    async def _insert(table: str, data: list[Any], **kwargs: Any) -> None:
        captured.extend(data)

    mock = MagicMock()
    mock.insert = _insert
    mock.query = AsyncMock(return_value=MagicMock(result_rows=[]))
    mock.command = AsyncMock(return_value=None)
    mock.ping = AsyncMock(return_value=True)
    return mock, captured


# ── Message 辅助（C-1 集成测试）──────────────────────────────────────────────


async def truncate_message_tables(client: Any | None = None) -> None:
    """清空 Message 四张表（集成/E2E 测试隔离）。"""
    if client is None:
        from app.core.clickhouse_client import get_clickhouse_client

        client = await get_clickhouse_client()
    from app.message.tables import (
        MESSAGE_DESTINATION_STATE,
        MESSAGE_DESTINATION_STATS_5M,
        MESSAGE_EVENTS,
        MESSAGE_LIFECYCLE,
    )

    db = TEST_CLICKHOUSE_DATABASE
    for table in (
        MESSAGE_EVENTS,
        MESSAGE_LIFECYCLE,
        MESSAGE_DESTINATION_STATE,
        MESSAGE_DESTINATION_STATS_5M,
    ):
        await client.command(f"TRUNCATE TABLE IF EXISTS `{db}`.`{table}`")


async def insert_message_events(rows: list[Any], client: Any | None = None) -> None:
    """直接向 message_events 插入测试行（绕过 Writer/去重）。"""
    if not rows:
        return
    if client is None:
        from app.core.clickhouse_client import get_clickhouse_client

        client = await get_clickhouse_client()
    from app.message.tables import INSERT_COLUMNS, MESSAGE_EVENTS

    data = [row.as_tuple() for row in rows]
    db = TEST_CLICKHOUSE_DATABASE
    await client.insert(f"`{db}`.`{MESSAGE_EVENTS}`", data, column_names=list(INSERT_COLUMNS))


def make_message_event_row(**overrides: Any) -> Any:
    """构造合法 message EventRow（可直接 insert_message_events 或 store.insert_events）。"""
    from app.message.events import EventRow

    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    message_id = str(uuid.uuid4())
    defaults: dict[str, Any] = {
        "log_id": str(uuid.uuid4()),
        "timestamp_ms": now_ms,
        "observed_at_ms": now_ms,
        "aic": "svc-sender-001",
        "trace_id": "",
        "correlation_id": "",
        "direction": "send",
        "event_type": "send",
        "system": "kafka",
        "destination_name": "my-topic",
        "destination_kind": "topic",
        "virtual_host": "/",
        "subscription_name": "",
        "consumer_group_name": "",
        "routing_key": "",
        "partition": None,
        "offset": None,
        "message_id": message_id,
        "lifecycle_key": f"mid:{message_id}",
        "payload_size_bytes": 0,
        "delivery_attempt": 1,
        "settlement_latency_ms": None,
        "settlement_reason": "",
        "error_code": "",
        "error_message": "",
        "attributes": {},
        "raw_log": "",
    }
    defaults.update(overrides)
    return EventRow(**defaults)
