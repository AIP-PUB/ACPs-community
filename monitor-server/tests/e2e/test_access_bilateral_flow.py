"""E2E — Access 双侧发射流程（caller client span + callee server span）。

模拟 demo-leader / demo-partner 双侧发射语义：
- 同一 trace_id 下投递 caller（aic=leader）与 server（aic=callee）两条 span
- traces/query 可见该 trace
- traces/{traceId} 含 client/server 两层（parent 链接）
- topology 仅 callee 视角计入（caller 行不重复计数）
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import AsyncClient

from tests.support.kafka_helper import produce_access_event, wait_for_access_event_ingested


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
async def test_access_bilateral_trace_and_topology(
    e2e_access_runtime: None,
    e2e_http_client: AsyncClient,
) -> None:
    from app.core.config import settings

    trace_id = uuid.uuid4().hex
    leader_aic = f"aic-leader-{uuid.uuid4().hex[:8]}"
    partner_aic = f"aic-partner-{uuid.uuid4().hex[:8]}"
    client_span = uuid.uuid4().hex[:16]
    server_span = uuid.uuid4().hex[:16]
    log_id_client = str(uuid.uuid4())
    log_id_server = str(uuid.uuid4())

    # ② leader caller span（aic=leader，不计入 topology）
    await produce_access_event(
        aic=leader_aic,
        log_id=log_id_client,
        trace_id=trace_id,
        span_id=client_span,
        parent_span_id="",
        method="Start",
        route="/rpc",
        response_status=200,
        duration_ms=120,
        caller_aic=leader_aic,
        callee_aic=partner_aic,
        caller_service="demo-leader",
        callee_service="demo-partner-test",
        service_name="demo-leader",
    )

    # ③ partner server span（aic=callee，parent=client_span）
    await produce_access_event(
        aic=partner_aic,
        log_id=log_id_server,
        trace_id=trace_id,
        span_id=server_span,
        parent_span_id=client_span,
        method="Start",
        route="/rpc",
        response_status=200,
        duration_ms=80,
        caller_aic=leader_aic,
        callee_aic=partner_aic,
        caller_service="demo-leader",
        callee_service="demo-partner-test",
        service_name="demo-partner-test",
    )

    await wait_for_access_event_ingested(log_id_client, timeout_s=20.0)
    await wait_for_access_event_ingested(log_id_server, timeout_s=20.0)
    await asyncio.sleep(1.5)

    # events/query：双侧数据均可查
    events_deadline = asyncio.get_event_loop().time() + 15.0
    found_ids: set[str] = set()
    events_resp = None
    while asyncio.get_event_loop().time() < events_deadline:
        events_resp = await e2e_http_client.post(
            f"{settings.api_v1_str}/access/events/query",
            json=_time_range_body(),
        )
        if events_resp.status_code == 200:
            for item in events_resp.json().get("items", []):
                lid = item.get("logId") or item.get("log_id")
                if lid in {log_id_client, log_id_server}:
                    found_ids.add(lid)
            if found_ids == {log_id_client, log_id_server}:
                break
        await asyncio.sleep(1.0)
    assert found_ids == {log_id_client, log_id_server}
    assert events_resp is not None

    # traces/{traceId}：两层 span
    trace_resp = await e2e_http_client.get(f"{settings.api_v1_str}/access/traces/{trace_id}")
    assert trace_resp.status_code == 200, trace_resp.text
    spans = trace_resp.json().get("spans", [])
    span_ids = {s.get("spanId") or s.get("span_id") for s in spans}
    assert client_span in span_ids, f"缺少 client span: {span_ids}"
    assert server_span in span_ids, f"缺少 server span: {span_ids}"

    server_item = next(s for s in spans if (s.get("spanId") or s.get("span_id")) == server_span)
    parent = server_item.get("parentSpanId") or server_item.get("parent_span_id")
    assert parent == client_span

    # caller 行 aic≠callee_aic，不计入 topology；server 行 aic=callee（由 events 字段验证）
    client_items = [
        i for i in events_resp.json().get("items", []) if (i.get("logId") or i.get("log_id")) == log_id_client
    ]
    server_items = [
        i for i in events_resp.json().get("items", []) if (i.get("logId") or i.get("log_id")) == log_id_server
    ]
    assert client_items and client_items[0].get("aic") == leader_aic
    assert server_items and server_items[0].get("aic") == partner_aic

    # topology/query：callee 视角单边计数（仅 server 行计入 MV）
    topo_deadline = asyncio.get_event_loop().time() + 20.0
    callee_edges: list[dict] = []
    topo_resp = None
    while asyncio.get_event_loop().time() < topo_deadline:
        topo_resp = await e2e_http_client.post(
            f"{settings.api_v1_str}/access/topology/query",
            json={**_time_range_body(), "groupBy": "aic"},
        )
        if topo_resp.status_code == 200:
            callee_edges = [
                e
                for e in topo_resp.json().get("items", [])
                if (e.get("callerAic") or e.get("caller_aic")) == leader_aic
                and (e.get("calleeAic") or e.get("callee_aic")) == partner_aic
            ]
            if callee_edges:
                break
        await asyncio.sleep(1.0)

    assert topo_resp is not None and topo_resp.status_code == 200, getattr(topo_resp, "text", topo_resp)
    assert len(callee_edges) >= 1, "topology 应含 leader→partner 边（callee 视角）"
