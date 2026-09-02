"""tests/integration/test_access_clickhouse_schema.py — DDL bootstrap 集成测试（C-1）。

需要真实 ClickHouse（dev-infra clickhouse 服务）。
不可达时自动 skip（_require_clickhouse session fixture）。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
async def _ch_guard(_require_clickhouse: None, clickhouse_schema: None) -> None:
    """本文件所有测试依赖真实 ClickHouse + schema 已建好。"""


class TestEnsureAccessSchemaIdempotent:
    async def test_idempotent_double_call(self) -> None:
        """两次调用 ensure_access_schema 不报错（CREATE IF NOT EXISTS）。"""
        from app.access.store import ensure_access_schema

        await ensure_access_schema()
        await ensure_access_schema()

    async def test_three_tables_exist(self) -> None:
        """三张主表全部存在。"""
        from app.access.tables import ACCESS_EVENTS, ACCESS_TOPOLOGY_EDGE_5M, ACCESS_TRACE_SPAN
        from app.core.clickhouse_client import get_clickhouse_client
        from tests.support.constants import TEST_CLICKHOUSE_DATABASE

        client = await get_clickhouse_client()
        result = await client.query(
            "SELECT name FROM system.tables WHERE database = {db:String}",
            parameters={"db": TEST_CLICKHOUSE_DATABASE},
        )
        table_names = {row[0] for row in result.result_rows}
        assert ACCESS_EVENTS in table_names, f"{ACCESS_EVENTS} not in {table_names}"
        assert ACCESS_TRACE_SPAN in table_names, f"{ACCESS_TRACE_SPAN} not in {table_names}"
        assert ACCESS_TOPOLOGY_EDGE_5M in table_names, f"{ACCESS_TOPOLOGY_EDGE_5M} not in {table_names}"

    async def test_two_materialized_views_exist(self) -> None:
        """两个 MV 全部存在（trace_span MV + topology MV）。"""
        from app.core.clickhouse_client import get_clickhouse_client
        from tests.support.constants import TEST_CLICKHOUSE_DATABASE

        client = await get_clickhouse_client()
        result = await client.query(
            "SELECT name FROM system.tables WHERE database = {db:String} AND engine = 'MaterializedView'",
            parameters={"db": TEST_CLICKHOUSE_DATABASE},
        )
        mv_names = {row[0] for row in result.result_rows}
        assert len(mv_names) >= 2, f"期望至少 2 个 MV，实际有 {mv_names}"

    async def test_insert_propagates_to_trace_span_mv(self) -> None:
        """向 access_events 插入 span 行后，trace_span MV 中有对应记录。"""
        from app.access.tables import ACCESS_TRACE_SPAN
        from app.core.clickhouse_client import get_clickhouse_client
        from tests.support.clickhouse_helper import insert_raw_events, make_access_event_row, truncate_access_tables
        from tests.support.constants import TEST_CLICKHOUSE_DATABASE

        await truncate_access_tables()
        trace_id = "trace-mv-test-001"
        row = make_access_event_row(trace_id=trace_id, span_id="span-001", response_status=200)
        await insert_raw_events([row])

        client = await get_clickhouse_client()
        # 强制 MV 合并（消除未合并状态干扰）
        db = TEST_CLICKHOUSE_DATABASE
        await client.command(f"OPTIMIZE TABLE `{db}`.`{ACCESS_TRACE_SPAN}` FINAL")
        result = await client.query(
            f"SELECT count() FROM `{db}`.`{ACCESS_TRACE_SPAN}` WHERE trace_id = {{tid:String}}",
            parameters={"tid": trace_id},
        )
        count = result.result_rows[0][0]
        assert count >= 1, f"trace_span MV 应有 ≥1 行，实际 {count}"
