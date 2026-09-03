"""tests/integration/message/test_message_store.py — store.py 真实 ClickHouse 集成测试（C-1）。

插入真实数据行后验证 store 读取接口的正确性。
需要真实 ClickHouse（dev-infra clickhouse 服务）。
"""

from __future__ import annotations

import time
import uuid

import pytest

from tests.support.clickhouse_helper import insert_message_events, make_message_event_row

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def _schema_and_clean(_require_clickhouse: None, isolated_message_clickhouse: None) -> None:
    """本文件所有测试依赖 CH schema 已建 + 测试前后清空 Message 表。"""


def _now_ms() -> int:
    return int(time.time() * 1000)


class TestEnsureMessageSchema:
    async def test_ensure_message_schema_idempotent(self) -> None:
        """第二次调用 ensure_message_schema 不抛异常。"""
        from app.message.store import ensure_message_schema

        await ensure_message_schema()
        await ensure_message_schema()


class TestInsertAndQueryEvents:
    async def test_insert_and_run_events_query(self) -> None:
        """插入 3 行后 run_events_query 返回 3 行。"""
        from app.message import sql as sql_mod
        from app.message.filters import ResolvedSort, WhereClause
        from app.message.store import insert_events, run_events_query

        rows = [make_message_event_row(system="kafka-int") for _ in range(3)]
        await insert_events(rows)

        now_ms = _now_ms()
        stmt = sql_mod.build_events_query(
            where=WhereClause(sql="", params={}),
            time_params={"_from": now_ms - 60_000, "_to": now_ms + 60_000},
            sort=[ResolvedSort(field="timestamp", column_or_alias="timestamp", order="desc")],
            keyset=None,
            limit=10,
            include_raw_log=False,
        )
        results = await run_events_query(stmt, limit=10, include_raw_log=False)
        assert len(results) == 3

    async def test_filter_by_system_via_where(self) -> None:
        """where 子句过滤 system 后只返回匹配行。"""
        from app.message import sql as sql_mod
        from app.message.filters import ResolvedSort, WhereClause
        from app.message.store import insert_events, run_events_query

        system_a = f"sys-{uuid.uuid4().hex[:8]}"
        system_b = f"sys-{uuid.uuid4().hex[:8]}"
        await insert_events(
            [
                make_message_event_row(system=system_a),
                make_message_event_row(system=system_b),
            ]
        )

        now_ms = _now_ms()
        stmt = sql_mod.build_events_query(
            where=WhereClause(sql="AND system = {sys_val:String}", params={"sys_val": system_a}),
            time_params={"_from": now_ms - 60_000, "_to": now_ms + 60_000},
            sort=[ResolvedSort(field="timestamp", column_or_alias="timestamp", order="desc")],
            keyset=None,
            limit=10,
            include_raw_log=False,
        )
        results = await run_events_query(stmt, limit=10, include_raw_log=False)
        assert len(results) == 1
        assert results[0].system == system_a

    async def test_insert_events_only_writes_main_table(self) -> None:
        """insert_events 只写 message_events，不写派生表（C-MESSAGE-MODEL-1）。"""
        from app.core.clickhouse_client import get_clickhouse_client
        from app.message.store import insert_events
        from app.message.tables import MESSAGE_EVENTS, MESSAGE_LIFECYCLE
        from tests.support.constants import TEST_CLICKHOUSE_DATABASE

        row = make_message_event_row()
        await insert_events([row])

        client = await get_clickhouse_client()
        db = TEST_CLICKHOUSE_DATABASE
        events_count = await client.query(
            f"SELECT count() FROM `{db}`.`{MESSAGE_EVENTS}` WHERE log_id = {{lid:String}}",
            parameters={"lid": row.log_id},
        )
        lifecycle_count = await client.query(f"SELECT count() FROM `{db}`.`{MESSAGE_LIFECYCLE}`")
        assert events_count.result_rows[0][0] == 1
        assert lifecycle_count.result_rows[0][0] == 0


class TestRecomputeLifecycles:
    async def test_recompute_lifecycles_from_events(self) -> None:
        """seed send+ack 后 recompute → message_lifecycle 有对应行。"""
        from app.core.clickhouse_client import get_clickhouse_client
        from app.message.store import insert_events, recompute_lifecycles
        from app.message.tables import MESSAGE_LIFECYCLE
        from tests.support.constants import TEST_CLICKHOUSE_DATABASE

        message_id = f"msg-{uuid.uuid4().hex[:8]}"
        lifecycle_key = f"mid:{message_id}"
        now_ms = _now_ms()
        await insert_events(
            [
                make_message_event_row(
                    message_id=message_id,
                    lifecycle_key=lifecycle_key,
                    event_type="send",
                    direction="send",
                    timestamp_ms=now_ms,
                    observed_at_ms=now_ms,
                ),
                make_message_event_row(
                    message_id=message_id,
                    lifecycle_key=lifecycle_key,
                    event_type="ack",
                    direction="receive",
                    timestamp_ms=now_ms + 10,
                    observed_at_ms=now_ms + 10,
                    settlement_latency_ms=42,
                ),
            ]
        )

        inserted = await recompute_lifecycles(
            [("kafka", "my-topic", "topic", "/", lifecycle_key)],
            compacted_at_ms=now_ms + 100,
        )
        assert inserted >= 1

        client = await get_clickhouse_client()
        db = TEST_CLICKHOUSE_DATABASE
        result = await client.query(
            f"SELECT count() FROM `{db}`.`{MESSAGE_LIFECYCLE}` WHERE lifecycle_key = {{lk:String}}",
            parameters={"lk": lifecycle_key},
        )
        assert result.result_rows[0][0] >= 1


class TestBypassInsertHelper:
    async def test_insert_message_events_helper(self) -> None:
        """insert_message_events 辅助函数可绕过 Writer 直接 seed。"""
        from app.core.clickhouse_client import get_clickhouse_client
        from app.message.tables import MESSAGE_EVENTS
        from tests.support.constants import TEST_CLICKHOUSE_DATABASE

        row = make_message_event_row()
        await insert_message_events([row])

        client = await get_clickhouse_client()
        db = TEST_CLICKHOUSE_DATABASE
        result = await client.query(
            f"SELECT count() FROM `{db}`.`{MESSAGE_EVENTS}` WHERE log_id = {{lid:String}}",
            parameters={"lid": row.log_id},
        )
        assert result.result_rows[0][0] == 1
