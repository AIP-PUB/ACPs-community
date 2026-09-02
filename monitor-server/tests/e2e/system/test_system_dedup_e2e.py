"""E2E — System upsert 幂等，无 Redis 去重（H-1 dedup 场景，C-SYSTEM-WRITE-6 / D-2）。

验收项：
- 同 log_id Kafka 重投 → OpenSearch 仅 1 文档（_id upsert）
- Redis 中无 amp:system:dedupe:* 键
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from tests.support.factory import make_system_log_record
from tests.support.kafka_helper import produce_system_event, wait_for_system_event_ingested


async def _assert_no_dedupe_keys(redis: Any) -> None:
    cursor = 0
    keys: list[bytes] = []
    while True:
        cursor, batch = await redis.scan(cursor, match="amp:system:dedupe:*", count=200)
        keys.extend(batch)
        if cursor == 0:
            break
    assert keys == [], f"不应存在 Redis 去重键，实际: {keys}"


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_same_log_id_kafka_replay_single_document(
    e2e_system_runtime: dict[str, Any],
    opensearch_client: Any,
) -> None:
    """同 log_id 写两次 → OpenSearch 仅 1 文档。"""
    from app.system import indices

    log_id = str(uuid.uuid4())
    record = make_system_log_record(log_id=log_id, message="dedup-e2e-test")

    for _ in range(2):
        await produce_system_event(record=record)

    await wait_for_system_event_ingested(log_id, timeout_s=30)
    await asyncio.sleep(2.0)

    resp = await opensearch_client.search(
        index=indices.INDEX_PATTERN,
        body={"query": {"term": {"log_id": {"value": log_id}}}, "size": 10},
        ignore_unavailable=True,
        allow_no_indices=True,
    )
    total = resp.get("hits", {}).get("total", {}).get("value", 0)
    assert total == 1, f"期望 1 条，实际 {total} 条（upsert 幂等失败）"


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_no_redis_dedupe_keys_after_upsert(
    e2e_system_runtime: dict[str, Any],
    redis_client: Any,
) -> None:
    """Writer 不写入 Redis 去重键（偏异 D-2）。"""
    log_id = str(uuid.uuid4())
    record = make_system_log_record(log_id=log_id, message="dedup-redis-check")

    for _ in range(2):
        await produce_system_event(record=record)

    await wait_for_system_event_ingested(log_id, timeout_s=30)
    await asyncio.sleep(2.0)
    await _assert_no_dedupe_keys(redis_client)
