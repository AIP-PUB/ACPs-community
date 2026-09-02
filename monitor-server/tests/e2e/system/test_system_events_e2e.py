"""E2E — System 写入链路 + events/query 基础场景（H-1 events 场景）。

验收项：
- produce → writer 索引 → events/query 命中
- message 恒非空（C-SYSTEM-WRITE-7）
- 缺省 severityNumber=0
- 关键词搜索、AIC 过滤、排序
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from httpx import AsyncClient

from tests.e2e.system.helpers import system_time_range_body
from tests.support.factory import make_system_log_record
from tests.support.kafka_helper import produce_system_event, wait_for_system_event_ingested


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_system_event_ingest_basic(
    e2e_system_runtime: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """单条事件写入后 events/query 可命中。"""
    from app.core.config import settings

    log_id = await produce_system_event(message="e2e-basic-test")
    await wait_for_system_event_ingested(log_id, timeout_s=30)

    resp = await e2e_http_client.post(
        f"{settings.api_v1_str}/system/events/query",
        json=system_time_range_body(log_id=log_id),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert any(item.get("logId") == log_id for item in data.get("items", []))
    assert "meta" in data


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_message_always_non_empty(
    e2e_system_runtime: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """摄取后 message 字段非空（C-SYSTEM-WRITE-7）。"""
    from app.core.config import settings

    log_id = await produce_system_event(message="message-nonempty-check")
    await wait_for_system_event_ingested(log_id, timeout_s=30)

    resp = await e2e_http_client.post(
        f"{settings.api_v1_str}/system/events/query",
        json=system_time_range_body(log_id=log_id),
    )
    assert resp.status_code == 200
    items = resp.json().get("items", [])
    assert len(items) == 1
    assert items[0].get("message"), "message 应恒非空"


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_default_severity_number_zero(
    e2e_system_runtime: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """未指定 severity_number 时缺省为 0。"""
    from app.core.config import settings

    record = make_system_log_record(message="severity-default-test")
    record.pop("severity_number", None)
    log_id = await produce_system_event(record=record)
    await wait_for_system_event_ingested(log_id, timeout_s=30)

    resp = await e2e_http_client.post(
        f"{settings.api_v1_str}/system/events/query",
        json=system_time_range_body(log_id=log_id),
    )
    assert resp.status_code == 200
    items = resp.json().get("items", [])
    assert len(items) == 1
    assert items[0].get("severityNumber") == 0


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_system_event_keyword_search(
    e2e_system_runtime: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """关键词搜索：含特定词的事件可被 keyword 命中。"""
    from app.core.config import settings

    unique_word = f"e2eSysKw{uuid.uuid4().hex[:10]}"
    log_id = await produce_system_event(message=f"system event {unique_word} captured")
    await wait_for_system_event_ingested(log_id, timeout_s=30)

    resp = await e2e_http_client.post(
        f"{settings.api_v1_str}/system/events/query",
        json=system_time_range_body(keyword=unique_word),
    )
    assert resp.status_code == 200
    items = resp.json().get("items", [])
    assert any(item.get("logId") == log_id for item in items)


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_system_event_filter_by_aic(
    e2e_system_runtime: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """AIC 过滤：多 AIC 事件 → filter 仅返回指定 AIC。"""
    from app.core.config import settings

    tag = uuid.uuid4().hex[:8]
    aic_target = f"aic-tgt-{tag}"
    aic_other = f"aic-oth-{tag}"

    log_id_target = await produce_system_event(aic=aic_target, message=f"target-{tag}")
    log_id_other = await produce_system_event(aic=aic_other, message=f"other-{tag}")

    await wait_for_system_event_ingested(log_id_target, timeout_s=30)
    await wait_for_system_event_ingested(log_id_other, timeout_s=30)

    resp = await e2e_http_client.post(
        f"{settings.api_v1_str}/system/events/query",
        json=system_time_range_body(aic=aic_target),
    )
    assert resp.status_code == 200
    items = resp.json().get("items", [])
    assert any(item.get("logId") == log_id_target for item in items)
    assert all(item.get("aic") == aic_target for item in items)


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_system_event_sort_order(
    e2e_system_runtime: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """sort: timestamp desc 与 asc 返回顺序应为互逆。"""
    from app.core.config import settings

    tag = uuid.uuid4().hex[:8]
    aic = f"aic-sort-{tag}"
    log_ids: list[str] = []

    for i in range(3):
        lid = await produce_system_event(aic=aic, message=f"sort-event-{i}")
        log_ids.append(lid)
        await asyncio.sleep(0.2)

    await wait_for_system_event_ingested(log_ids[-1], timeout_s=30)

    base = system_time_range_body(aic=aic, limit=10)

    resp_desc = await e2e_http_client.post(
        f"{settings.api_v1_str}/system/events/query",
        json={**base, "sort": [{"field": "timestamp", "order": "desc"}]},
    )
    assert resp_desc.status_code == 200
    ts_desc = [item["timestamp"] for item in resp_desc.json().get("items", [])]

    resp_asc = await e2e_http_client.post(
        f"{settings.api_v1_str}/system/events/query",
        json={**base, "sort": [{"field": "timestamp", "order": "asc"}]},
    )
    assert resp_asc.status_code == 200
    ts_asc = [item["timestamp"] for item in resp_asc.json().get("items", [])]

    assert len(ts_desc) >= 2 and len(ts_asc) >= 2
    assert ts_desc[0] >= ts_desc[-1]
    assert ts_asc[0] <= ts_asc[-1]
    assert ts_desc == list(reversed(ts_asc))
