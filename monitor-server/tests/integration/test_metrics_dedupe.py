"""tests/integration/test_metrics_dedupe.py — dedupe.py 集成测试（Step 4）。

需要 Redis 7+ 在 localhost:6379 可用（测试库 db=3）。
运行：just test integration -k metrics_dedupe
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from app.metrics.dedupe import claim_log_ids, release_log_ids
from tests.support.redis_helper import reset_metrics_redis_state

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
async def redis_client():
    from app.core.redis_client import close_redis, get_redis

    r = get_redis()
    yield r
    await close_redis()


@pytest.fixture(autouse=True)
async def isolated_redis(redis_client: object) -> AsyncGenerator[None]:
    await reset_metrics_redis_state(redis_client)  # type: ignore[arg-type]
    yield
    await reset_metrics_redis_state(redis_client)  # type: ignore[arg-type]


# ── claim_log_ids ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claim_first_time_succeeds(redis_client: object) -> None:
    """首次 claim → 返回全部 log_ids（C-METRIC-WRITE-4）。"""
    ids = ["id-001", "id-002", "id-003"]
    claimed = await claim_log_ids(redis_client, ids)  # type: ignore[arg-type]
    assert claimed == set(ids)


@pytest.mark.asyncio
async def test_claim_duplicate_returns_empty(redis_client: object) -> None:
    """重复 claim 同一 log_id → 返回空集（幂等）。"""
    ids = ["dup-001"]
    await claim_log_ids(redis_client, ids)  # type: ignore[arg-type]
    claimed2 = await claim_log_ids(redis_client, ids)  # type: ignore[arg-type]
    assert claimed2 == set()


@pytest.mark.asyncio
async def test_claim_partial_dedup(redis_client: object) -> None:
    """部分重复 → 只返回新 id。"""
    await claim_log_ids(redis_client, ["already"])  # type: ignore[arg-type]
    claimed = await claim_log_ids(redis_client, ["already", "new-001"])  # type: ignore[arg-type]
    assert claimed == {"new-001"}


@pytest.mark.asyncio
async def test_claim_sets_ttl(redis_client: object) -> None:
    """claim 后 key 应设置 TTL（dedupe_ttl_seconds）。"""

    ids = ["ttl-001"]
    await claim_log_ids(redis_client, ids)  # type: ignore[arg-type]

    # 检查去重 key 是否存在（格式：amp:metrics:dedupe:<log_id>）
    ttl = await redis_client.ttl("amp:metrics:dedupe:ttl-001")  # type: ignore[attr-defined]
    assert ttl > 0


# ── release_log_ids ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_release_allows_reclaim(redis_client: object) -> None:
    """release 后可再次 claim（崩溃恢复语义，C-METRIC-WRITE-4）。"""
    ids = ["rel-001"]
    await claim_log_ids(redis_client, ids)  # type: ignore[arg-type]
    await release_log_ids(redis_client, ids)  # type: ignore[arg-type]
    claimed = await claim_log_ids(redis_client, ids)  # type: ignore[arg-type]
    assert claimed == set(ids)


@pytest.mark.asyncio
async def test_release_nonexistent_is_noop(redis_client: object) -> None:
    """释放不存在的 id 不报错。"""
    await release_log_ids(redis_client, ["ghost-id"])  # type: ignore[arg-type]
