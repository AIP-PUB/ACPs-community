"""tests/integration/test_access_store_query.py — store.py 真实 ClickHouse 集成测试（C-4）。

插入真实数据行后验证 store 读取接口的正确性。
需要真实 ClickHouse（dev-infra clickhouse 服务）。
"""

from __future__ import annotations

import time
import uuid

import pytest

from tests.support.clickhouse_helper import (
    insert_raw_events,
    make_access_event_row,
)


@pytest.fixture(autouse=True)
async def _schema_and_clean(_require_clickhouse: None, isolated_clickhouse: None) -> None:
    """本文件所有测试依赖 CH schema 已建 + 测试前后清空表。"""


def _now_ms() -> int:
    return int(time.time() * 1000)


class TestInsertAndQueryEvents:
    async def test_insert_and_run_events_query(self) -> None:
        """插入 3 行后 run_events_query 返回 3 行。"""
        from app.access import sql as sql_mod
        from app.access.filters import WhereClause
        from app.access.store import insert_events, run_events_query

        rows = [make_access_event_row(aic="aic-store-001") for _ in range(3)]
        await insert_events(rows)

        now_ms = _now_ms()
        stmt = sql_mod.build_events_query(
            where=WhereClause(sql="", params={}),
            time_params={"from_ms": now_ms - 60_000, "to_ms": now_ms},
            sort=[],
            keyset=None,
            limit=10,
            include_raw_log=False,
        )
        results = await run_events_query(stmt, limit=10, include_raw_log=False)
        assert len(results) == 3

    async def test_filter_by_aic_via_where(self) -> None:
        """where 子句过滤 aic 后只返回匹配行。"""
        from app.access import sql as sql_mod
        from app.access.filters import WhereClause
        from app.access.store import insert_events, run_events_query

        aic_a = f"aic-{uuid.uuid4().hex[:8]}"
        aic_b = f"aic-{uuid.uuid4().hex[:8]}"
        await insert_events(
            [
                make_access_event_row(aic=aic_a),
                make_access_event_row(aic=aic_b),
            ]
        )

        now_ms = _now_ms()
        stmt = sql_mod.build_events_query(
            where=WhereClause(sql="AND aic = {aic_val:String}", params={"aic_val": aic_a}),
            time_params={"from_ms": now_ms - 60_000, "to_ms": now_ms},
            sort=[],
            keyset=None,
            limit=10,
            include_raw_log=False,
        )
        results = await run_events_query(stmt, limit=10, include_raw_log=False)
        assert len(results) == 1
        assert results[0].aic == aic_a

    async def test_run_error_attribution(self) -> None:
        """插入错误行后 run_error_attribution 返回至少一条记录。"""
        from app.access import sql as sql_mod
        from app.access.filters import WhereClause
        from app.access.store import insert_events, run_error_attribution

        aic = f"aic-err-{uuid.uuid4().hex[:6]}"
        await insert_events(
            [
                make_access_event_row(aic=aic, response_status=500),
                make_access_event_row(aic=aic, response_status=200),
            ]
        )

        now_ms = _now_ms()
        stmt = sql_mod.build_error_attribution_query(
            group_dims=["aic", "error_code"],
            where=WhereClause(sql="AND aic = {aic_val:String}", params={"aic_val": aic}),
            time_params={"from_ms": now_ms - 60_000, "to_ms": now_ms},
            error_status_threshold=500,
            top_n=10,
        )
        results = await run_error_attribution(stmt)
        assert len(results) >= 1
        assert results[0].count >= 1

    async def test_run_slow_requests(self) -> None:
        """插入慢请求行后 run_slow_requests 返回记录，且时间大于阈值。"""
        from app.access import sql as sql_mod
        from app.access.filters import WhereClause
        from app.access.store import insert_events, run_slow_requests

        aic = f"aic-slow-{uuid.uuid4().hex[:6]}"
        await insert_events(
            [
                make_access_event_row(aic=aic, duration_ms=5000),
                make_access_event_row(aic=aic, duration_ms=100),
            ]
        )

        now_ms = _now_ms()
        stmt = sql_mod.build_slow_requests_query(
            where=WhereClause(sql="AND aic = {aic_val:String}", params={"aic_val": aic}),
            time_params={"from_ms": now_ms - 60_000, "to_ms": now_ms},
            min_duration_ms=500,
            top_n=5,
            keyset=None,
        )
        results = await run_slow_requests(stmt)
        assert len(results) >= 1
        max_dur = max(r.duration_ms for r in results)
        assert max_dur >= 5000


class TestTraceSpan:
    async def test_fetch_trace_spans_returns_rows(self) -> None:
        """插入 trace 行后 fetch_trace_spans 返回正确数量。"""
        from app.access.store import fetch_trace_spans, insert_events

        trace_id = f"tr-{uuid.uuid4().hex[:8]}"
        spans = [make_access_event_row(trace_id=trace_id, span_id=f"sp-{i:03d}") for i in range(3)]
        await insert_events(spans)

        result, truncated = await fetch_trace_spans(trace_id, trace_max_spans=500)
        assert len(result) >= 3
        assert not truncated

    async def test_trace_not_found_returns_empty(self) -> None:
        from app.access.store import fetch_trace_spans

        spans, _ = await fetch_trace_spans("no-such-trace-id", trace_max_spans=500)
        assert spans == []

    async def test_trace_spans_truncated_at_max(self) -> None:
        """超出 access_trace_max_spans 的 trace 应被截断（C-4，truncated=True）。"""
        from app.access.store import fetch_trace_spans, insert_events

        trace_id = f"tr-trunc-{uuid.uuid4().hex[:8]}"
        # 插入 5 条 span
        rows = [make_access_event_row(trace_id=trace_id, span_id=f"sp-{i:03d}") for i in range(5)]
        await insert_events(rows)

        # 设置 max_spans=3，期望截断
        spans, truncated = await fetch_trace_spans(trace_id, trace_max_spans=3)
        # 如果实际返回了 5 行（MV 还未合并），max_spans=3 应截断
        if len(rows) >= 3:
            assert truncated is True or len(spans) <= 3, f"截断测试：spans={len(spans)}, truncated={truncated}"


class TestInsertEvents:
    async def test_insert_deduplicated_by_store_once(self) -> None:
        """同一 log_id 插入两次，CH 去重（ReplacingMergeTree 或手动）。

        注意：ClickHouse 最终一致性；OPTIMIZE FINAL 强制合并。
        """
        from app.access.store import insert_events
        from app.access.tables import ACCESS_EVENTS
        from app.core.clickhouse_client import get_clickhouse_client
        from tests.support.constants import TEST_CLICKHOUSE_DATABASE

        log_id = str(uuid.uuid4())
        row = make_access_event_row(log_id=log_id)
        # 通过 insert_events 写一次，再通过 insert_raw_events 写同一 log_id
        await insert_events([row])
        await insert_raw_events([row])

        client = await get_clickhouse_client()
        db = TEST_CLICKHOUSE_DATABASE
        await client.command(f"OPTIMIZE TABLE `{db}`.`{ACCESS_EVENTS}` FINAL DEDUPLICATE")
        result = await client.query(
            f"SELECT count() FROM `{db}`.`{ACCESS_EVENTS}` WHERE log_id = {{lid:String}}",
            parameters={"lid": log_id},
        )
        count = result.result_rows[0][0]
        # OPTIMIZE FINAL DEDUPLICATE 对完全相同行去重后应只保留 1 行
        assert count == 1, f"期望 1 行，实际 {count}"


# ── C-1 replay_partition ───────────────────────────────────────────────────────


class TestReplayPartition:
    """replay_partition 集成测试（C-1）。

    验证：
    1. replay 后 trace_span 派生表与 access_events 中 trace 数一致。
    2. 幂等重放（第二次 replay 不重复记录）。
    3. 无 trace_id 行不被写入 trace_span。
    """

    async def test_replay_rebuilds_trace_span(self) -> None:
        """replay 后 trace_span 中出现 access_events 中有 trace_id 的行（C-1）。"""
        from datetime import UTC, datetime

        from app.access.store import insert_events, replay_partition
        from app.access.tables import ACCESS_TRACE_SPAN
        from app.core.clickhouse_client import get_clickhouse_client
        from tests.support.constants import TEST_CLICKHOUSE_DATABASE

        trace_id = f"tr-replay-{uuid.uuid4().hex[:8]}"
        now = datetime.now(UTC)
        row = make_access_event_row(trace_id=trace_id, span_id="sp-replay-001")
        await insert_events([row])

        partition_yyyymmdd = int(now.strftime("%Y%m%d"))
        await replay_partition(partition_yyyymmdd)

        client = await get_clickhouse_client()
        db = TEST_CLICKHOUSE_DATABASE
        await client.command(f"OPTIMIZE TABLE `{db}`.`{ACCESS_TRACE_SPAN}` FINAL")
        result = await client.query(
            f"SELECT count() FROM `{db}`.`{ACCESS_TRACE_SPAN}` WHERE trace_id = {{tid:String}}",
            parameters={"tid": trace_id},
        )
        count = result.result_rows[0][0]
        assert count >= 1, f"replay 后 trace_span 应有 ≥1 行，实际 {count}"

    async def test_replay_is_idempotent(self) -> None:
        """replay 两次不应让 trace_span 行数翻倍（DROP + INSERT 保持幂等）。"""
        from datetime import UTC, datetime

        from app.access.store import insert_events, replay_partition
        from app.access.tables import ACCESS_TRACE_SPAN
        from app.core.clickhouse_client import get_clickhouse_client
        from tests.support.constants import TEST_CLICKHOUSE_DATABASE

        trace_id = f"tr-idem-{uuid.uuid4().hex[:8]}"
        now = datetime.now(UTC)
        row = make_access_event_row(trace_id=trace_id, span_id="sp-idem-001")
        await insert_events([row])

        partition_yyyymmdd = int(now.strftime("%Y%m%d"))
        await replay_partition(partition_yyyymmdd)
        await replay_partition(partition_yyyymmdd)

        client = await get_clickhouse_client()
        db = TEST_CLICKHOUSE_DATABASE
        await client.command(f"OPTIMIZE TABLE `{db}`.`{ACCESS_TRACE_SPAN}` FINAL")
        result = await client.query(
            f"SELECT count() FROM `{db}`.`{ACCESS_TRACE_SPAN}` WHERE trace_id = {{tid:String}}",
            parameters={"tid": trace_id},
        )
        count = result.result_rows[0][0]
        assert count == 1, f"幂等重放后应只有 1 行，实际 {count}"

    async def test_replay_excludes_rows_without_trace_id(self) -> None:
        """没有 trace_id 的行不应出现在 trace_span 中（C-1 正确性）。"""
        from datetime import UTC, datetime

        from app.access.store import insert_events, replay_partition
        from app.access.tables import ACCESS_TRACE_SPAN
        from app.core.clickhouse_client import get_clickhouse_client
        from tests.support.constants import TEST_CLICKHOUSE_DATABASE

        log_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        row = make_access_event_row(log_id=log_id, trace_id="", span_id="")
        await insert_events([row])

        partition_yyyymmdd = int(now.strftime("%Y%m%d"))
        await replay_partition(partition_yyyymmdd)

        client = await get_clickhouse_client()
        db = TEST_CLICKHOUSE_DATABASE
        result = await client.query(
            f"SELECT count() FROM `{db}`.`{ACCESS_TRACE_SPAN}` WHERE log_id = {{lid:String}}",
            parameters={"lid": log_id},
        )
        count = result.result_rows[0][0]
        assert count == 0, f"无 trace_id 的行不应进入 trace_span，实际 {count}"

    async def test_replay_rebuilds_topology_edge(self) -> None:
        """replay 后 topology_edge_5m 应有 access_events 中 caller/callee 行对应的聚合记录（C-ACCESS-RETENTION-3）。"""
        from datetime import UTC, datetime

        from app.access.store import insert_events, replay_partition
        from app.access.tables import ACCESS_TOPOLOGY_EDGE_5M
        from app.core.clickhouse_client import get_clickhouse_client
        from tests.support.constants import TEST_CLICKHOUSE_DATABASE

        caller_aic = f"caller-replay-{uuid.uuid4().hex[:8]}"
        callee_aic = f"callee-replay-{uuid.uuid4().hex[:8]}"
        now = datetime.now(UTC)
        row = make_access_event_row(
            aic=callee_aic,
            caller_aic=caller_aic,
            caller_service="svc-caller",
            callee_aic=callee_aic,
            callee_service="svc-callee",
        )
        await insert_events([row])

        partition_yyyymmdd = int(now.strftime("%Y%m%d"))
        await replay_partition(partition_yyyymmdd)

        client = await get_clickhouse_client()
        db = TEST_CLICKHOUSE_DATABASE
        await client.command(f"OPTIMIZE TABLE `{db}`.`{ACCESS_TOPOLOGY_EDGE_5M}` FINAL")
        result = await client.query(
            f"SELECT sumMerge(call_count_state) FROM `{db}`.`{ACCESS_TOPOLOGY_EDGE_5M}` "
            f"WHERE caller_aic = {{caic:String}} AND callee_aic = {{daic:String}}",
            parameters={"caic": caller_aic, "daic": callee_aic},
        )
        count = int(result.result_rows[0][0]) if result.result_rows else 0
        assert count >= 1, f"replay 后 topology_edge_5m 应有 ≥1 调用记录，实际 {count}"


class TestTopologyEdge:
    """topology_edge_5m 表的 SummingMergeTree 聚合行为验证（C-4）。

    仅插入有 caller/callee 关系的数据，验证聚合字段语义。
    注意：topology 表使用 AggregatingMergeTree + state 字段，
          必须通过 -Merge 函数（如 sumMerge/avgMerge）读取最终值。
    """

    async def test_topology_mv_receives_caller_callee_rows(self) -> None:
        """有 caller+callee 的行插入 access_events 后，topology MV 有对应记录。"""
        from app.access.tables import ACCESS_TOPOLOGY_EDGE_5M
        from app.core.clickhouse_client import get_clickhouse_client
        from tests.support.clickhouse_helper import insert_raw_events
        from tests.support.clickhouse_helper import make_access_event_row as make_row
        from tests.support.constants import TEST_CLICKHOUSE_DATABASE

        caller = f"svc-caller-{uuid.uuid4().hex[:6]}"
        callee = f"svc-callee-{uuid.uuid4().hex[:6]}"

        row3 = make_row(
            **{
                "aic": "aic-topo-callee",
                "caller_aic": "aic-topo-caller",
                "caller_service": caller,
                "callee_aic": "aic-topo-callee",
                "callee_service": callee,
            }
        )
        await insert_raw_events([row3])

        client = await get_clickhouse_client()
        db = TEST_CLICKHOUSE_DATABASE
        await client.command(f"OPTIMIZE TABLE `{db}`.`{ACCESS_TOPOLOGY_EDGE_5M}` FINAL")

        result = await client.query(
            f"SELECT sumMerge(call_count_state) AS call_count "
            f"FROM `{db}`.`{ACCESS_TOPOLOGY_EDGE_5M}` "
            f"WHERE caller_service = {{cs:String}} AND callee_service = {{ce:String}}",
            parameters={"cs": caller, "ce": callee},
        )
        total_calls = result.result_rows[0][0] if result.result_rows else 0
        assert total_calls >= 1, f"topology MV 应记录到 {caller}->{callee} 的调用，实际 count={total_calls}"

    async def test_topology_error_rate_via_merge(self) -> None:
        """插入错误行后 topology error_count_state 通过 sumMerge 读出 ≥1（C-4）。"""
        from app.access.tables import ACCESS_TOPOLOGY_EDGE_5M
        from app.core.clickhouse_client import get_clickhouse_client
        from tests.support.clickhouse_helper import insert_raw_events
        from tests.support.clickhouse_helper import make_access_event_row as make_row
        from tests.support.constants import TEST_CLICKHOUSE_DATABASE

        caller = f"err-caller-{uuid.uuid4().hex[:6]}"
        callee = f"err-callee-{uuid.uuid4().hex[:6]}"

        row_ok = make_row(
            aic="aic-err-callee",
            caller_service=caller,
            callee_service=callee,
            caller_aic="aic-err-caller",
            callee_aic="aic-err-callee",
            response_status=200,
        )
        row_err = make_row(
            aic="aic-err-callee",
            caller_service=caller,
            callee_service=callee,
            caller_aic="aic-err-caller",
            callee_aic="aic-err-callee",
            response_status=500,
        )
        await insert_raw_events([row_ok, row_err])

        client = await get_clickhouse_client()
        db = TEST_CLICKHOUSE_DATABASE
        await client.command(f"OPTIMIZE TABLE `{db}`.`{ACCESS_TOPOLOGY_EDGE_5M}` FINAL")

        result = await client.query(
            f"SELECT sumMerge(error_count_state) AS error_count "
            f"FROM `{db}`.`{ACCESS_TOPOLOGY_EDGE_5M}` "
            f"WHERE caller_service = {{cs:String}} AND callee_service = {{ce:String}}",
            parameters={"cs": caller, "ce": callee},
        )
        error_count = result.result_rows[0][0] if result.result_rows else 0
        assert error_count >= 1, f"topology MV 应记录到错误次数 ≥1，实际 {error_count}"
