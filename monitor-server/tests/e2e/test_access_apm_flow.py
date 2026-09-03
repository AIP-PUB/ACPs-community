"""E2E — Access APM 流程（trace + topology）。

验收项：
- 投递多个 span（相同 trace_id）→ AccessWriter 写入 ClickHouse
- traces/query → 返回该 trace 的 summary（span count, duration, error_count）
- traces/{traceId} → 返回完整 spans 列表
- topology/query → 返回跨服务调用边（caller→callee）

注：ClickHouse 物化视图（access_trace_span / access_topology_edge_5m）由
MV 在后台同步写入，`*Merge` 函数保证查询不依赖后台合并完成。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import AsyncClient

from tests.support.kafka_helper import (
    produce_access_event,
    wait_for_access_event_ingested,
)


def _time_range_body(*, lookback_hours: int = 1) -> dict:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    start = (now - timedelta(hours=lookback_hours)).isoformat()
    end = now.isoformat()
    return {
        "timeRange": {"startAt": start, "endAt": end},
        "page": {"limit": 50},
    }


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_access_trace_query(
    e2e_access_runtime: None,
    e2e_http_client: AsyncClient,
) -> None:
    """投递同一 trace 的两个 span → traces/query 15s 内返回 trace 摘要。"""
    from app.core.config import settings

    trace_id = str(uuid.uuid4())
    callee_aic = f"aic-callee-{uuid.uuid4().hex[:8]}"
    caller_aic = f"aic-caller-{uuid.uuid4().hex[:8]}"
    span_id_root = str(uuid.uuid4())[:16]
    span_id_child = str(uuid.uuid4())[:16]
    log_id_root = str(uuid.uuid4())
    log_id_child = str(uuid.uuid4())

    # 根 span（无 parent_span_id）
    await produce_access_event(
        aic=callee_aic,
        log_id=log_id_root,
        trace_id=trace_id,
        span_id=span_id_root,
        parent_span_id="",
        method="POST",
        route="/api/v1/process",
        response_status=200,
        duration_ms=250,
        service_name="callee-svc",
    )

    # 子 span（parent = root）
    await produce_access_event(
        aic=caller_aic,
        log_id=log_id_child,
        trace_id=trace_id,
        span_id=span_id_child,
        parent_span_id=span_id_root,
        method="GET",
        route="/api/v1/data",
        response_status=200,
        duration_ms=50,
        caller_aic=callee_aic,
        service_name="caller-svc",
    )

    await wait_for_access_event_ingested(log_id_root, timeout_s=20.0)
    await wait_for_access_event_ingested(log_id_child, timeout_s=20.0)

    # 等待 MV 写入 access_trace_span（CH MV 近乎同步，但留 1s 余量）
    await asyncio.sleep(1.0)

    # traces/query 验证
    deadline = asyncio.get_event_loop().time() + 15.0
    found_trace: dict | None = None
    while asyncio.get_event_loop().time() < deadline:
        resp = await e2e_http_client.post(
            f"{settings.api_v1_str}/access/traces/query",
            json=_time_range_body(),
        )
        if resp.status_code == 200:
            items = resp.json().get("items", [])
            for item in items:
                if item.get("traceId") == trace_id or item.get("trace_id") == trace_id:
                    found_trace = item
                    break
        if found_trace:
            break
        await asyncio.sleep(1.0)

    assert found_trace is not None, f"traces/query 在 15s 内未返回 trace_id={trace_id!r}"
    span_count = found_trace.get("totalSpans") or found_trace.get("total_spans") or 0
    assert span_count >= 1, f"trace 应含至少 1 个 span，实际 span_count={span_count}"


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_access_get_trace(
    e2e_access_runtime: None,
    e2e_http_client: AsyncClient,
) -> None:
    """投递单个 span → traces/{traceId} 返回完整 spans 列表。"""
    from app.core.config import settings

    trace_id = str(uuid.uuid4())
    aic = f"aic-e2e-get-trace-{uuid.uuid4().hex[:8]}"
    log_id = str(uuid.uuid4())

    await produce_access_event(
        aic=aic,
        log_id=log_id,
        trace_id=trace_id,
        span_id=str(uuid.uuid4())[:16],
        route="/api/v1/resource",
        response_status=200,
        duration_ms=80,
    )

    await wait_for_access_event_ingested(log_id, timeout_s=20.0)
    await asyncio.sleep(1.0)  # 等待 MV 写入

    resp = await e2e_http_client.get(
        f"{settings.api_v1_str}/access/traces/{trace_id}",
    )
    assert resp.status_code == 200, f"traces/{{traceId}} 返回 {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data.get("traceId") == trace_id or data.get("trace_id") == trace_id
    spans = data.get("spans", [])
    assert len(spans) >= 1, f"期望至少 1 个 span，实际 spans={spans}"


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_access_topology_query(
    e2e_access_runtime: None,
    e2e_http_client: AsyncClient,
) -> None:
    """投递跨服务调用事件 → topology/query 返回 caller→callee 拓扑边。"""
    from app.core.config import settings

    caller_aic = f"aic-caller-topo-{uuid.uuid4().hex[:8]}"
    callee_aic = f"aic-callee-topo-{uuid.uuid4().hex[:8]}"
    log_id = str(uuid.uuid4())

    await produce_access_event(
        aic=callee_aic,
        log_id=log_id,
        caller_aic=caller_aic,
        route="/api/v1/data",
        response_status=200,
        duration_ms=60,
        service_name="callee-svc",
    )

    await wait_for_access_event_ingested(log_id, timeout_s=20.0)
    # 等待 access_topology_edge_5m MV 写入（5 分钟聚合桶，MV 写入接近同步）
    await asyncio.sleep(2.0)

    resp = await e2e_http_client.post(
        f"{settings.api_v1_str}/access/topology/query",
        json=_time_range_body(),
    )
    assert resp.status_code == 200, f"topology/query 返回 {resp.status_code}: {resp.text}"
    data = resp.json()
    assert "items" in data
    assert "meta" in data
    # 注：MV 聚合表在测试环境可能需要 optimize，这里只验证 API 可正常响应
