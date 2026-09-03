"""E2E — System 保守水位与滞后降级（H-1 freshness 场景，D-5）。

验收项：
- meta.dataFreshnessAt 保守（< max(event.timestamp)）
- 无水位时 lagging_response_mode=503 → AMP_READ_MODEL_LAGGING
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient

from app.system.freshness import WM_INGEST_PARTITIONS, WM_INGEST_PREFIX
from tests.e2e.system.helpers import system_time_range_body
from tests.support.factory import make_system_log_record
from tests.support.kafka_helper import produce_system_event, wait_for_system_event_ingested
from tests.support.redis_helper import reset_system_redis_state


def _parse_iso_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_conservative_watermark_below_max_event_timestamp(
    e2e_system_runtime: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """摄取后压低水位 → dataFreshnessAt < max(事件 timestamp)（保守语义）。"""
    from app.core.config import settings

    tag = "fresh-conservative"
    aic = f"aic-{tag}"
    t_recent = datetime.now(UTC)
    t_older = t_recent - timedelta(minutes=30)

    last_id = ""
    for ts in (t_older, t_recent):
        record = make_system_log_record(
            aic=aic,
            message=f"{tag}-{ts.isoformat()}",
            timestamp=ts.isoformat(),
        )
        last_id = await produce_system_event(record=record)
    await wait_for_system_event_ingested(last_id, timeout_s=30)
    await asyncio.sleep(settings.system_bulk_index_batch_interval_seconds + 0.5)

    redis = e2e_system_runtime["redis"]
    max_ts_ms = int(t_recent.timestamp() * 1000)
    conservative_wm = max_ts_ms - 200
    partitions = await redis.smembers(WM_INGEST_PARTITIONS)
    if not partitions:
        await redis.sadd(WM_INGEST_PARTITIONS, "0")
        partitions = {"0"}
    for raw_pid in partitions:
        pid = raw_pid.decode() if isinstance(raw_pid, bytes) else str(raw_pid)
        await redis.set(f"{WM_INGEST_PREFIX}{pid}", str(conservative_wm))

    resp = await e2e_http_client.post(
        f"{settings.api_v1_str}/system/events/query",
        json=system_time_range_body(aic=aic),
    )
    assert resp.status_code in (200, 206)
    data = resp.json()
    freshness_at = data.get("meta", {}).get("dataFreshnessAt")
    assert freshness_at is not None

    items = data.get("items", [])
    assert len(items) >= 2
    max_event_ms = max(_parse_iso_ms(item["timestamp"]) for item in items)
    freshness_ms = _parse_iso_ms(freshness_at)
    assert freshness_ms < max_event_ms, (
        f"保守水位应 < max(timestamp)：freshness={freshness_ms}, max_event={max_event_ms}"
    )


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_no_watermark_returns_read_model_lagging_503(
    e2e_http_client: AsyncClient,
    redis_client: Any,
) -> None:
    """无摄取水位 + lagging_response_mode=503 → AMP_READ_MODEL_LAGGING。"""
    from app.core.config import settings
    from app.system.exception import SystemErrorCode
    from tests.support.opensearch_helper import create_test_index

    assert settings.system_lagging_response_mode == "503"
    await reset_system_redis_state(redis_client)
    await create_test_index()

    resp = await e2e_http_client.post(
        f"{settings.api_v1_str}/system/events/query",
        json=system_time_range_body(),
    )
    assert resp.status_code == 503
    assert resp.json().get("error_code") == SystemErrorCode.READ_MODEL_LAGGING
