"""tests/integration/test_metrics_snapshot_cache.py — snapshot_cache.py 集成测试（Step 4）。

需要 Redis 7+ 在 localhost:6379 可用（测试库 db=3）。
运行：just test integration -k metrics_snapshot_cache
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from app.metrics.snapshot_cache import (
    SNAPSHOT_INDEX_KEY,
    CachedSnapshot,
    get_snapshot,
    mget_snapshots,
    remove_index_entry,
    scan_index_desc,
    upsert_snapshot,
)
from tests.support.redis_helper import reset_metrics_redis_state, seed_snapshot

pytestmark = pytest.mark.integration

BASE_MS = 1_700_000_000_000


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


# ── upsert_snapshot ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_creates_hash_and_zset(redis_client: object) -> None:
    """upsert → Hash 和 ZSet 索引均存在。"""
    snap = CachedSnapshot(
        aic="aic-001",
        observed_at_ms=BASE_MS,
        uptime_seconds=100.0,
        load_metrics=None,
        window_metrics=None,
        service_name="svc",
        service_namespace="ns",
        deployment_env="dev",
    )
    await upsert_snapshot(redis_client, snap)  # type: ignore[arg-type]

    # Hash 存在
    result = await get_snapshot(redis_client, "aic-001")  # type: ignore[arg-type]
    assert result is not None
    assert result.aic == "aic-001"
    assert result.observed_at_ms == BASE_MS

    # ZSet 索引存在
    score = await redis_client.zscore(SNAPSHOT_INDEX_KEY, "aic-001")  # type: ignore[attr-defined]
    assert score is not None
    assert int(score) == BASE_MS


@pytest.mark.asyncio
async def test_upsert_later_event_updates(redis_client: object) -> None:
    """较新的 observed_at → 覆盖旧值。"""
    await seed_snapshot(redis_client, aic="aic-002", observed_at_ms=BASE_MS)  # type: ignore[arg-type]
    newer_snap = CachedSnapshot(
        aic="aic-002",
        observed_at_ms=BASE_MS + 1000,
        uptime_seconds=200.0,
        load_metrics=None,
        window_metrics=None,
        service_name=None,
        service_namespace=None,
        deployment_env=None,
    )
    await upsert_snapshot(redis_client, newer_snap)  # type: ignore[arg-type]
    result = await get_snapshot(redis_client, "aic-002")  # type: ignore[arg-type]
    assert result is not None
    assert result.observed_at_ms == BASE_MS + 1000


@pytest.mark.asyncio
async def test_upsert_older_event_does_not_rollback(redis_client: object) -> None:
    """迟到事件（较小的 observed_at）不回退最新快照（ZADD GT 语义）。"""
    await seed_snapshot(redis_client, aic="aic-003", observed_at_ms=BASE_MS)  # type: ignore[arg-type]
    older_snap = CachedSnapshot(
        aic="aic-003",
        observed_at_ms=BASE_MS - 5000,
        uptime_seconds=50.0,
        load_metrics=None,
        window_metrics=None,
        service_name=None,
        service_namespace=None,
        deployment_env=None,
    )
    await upsert_snapshot(redis_client, older_snap)  # type: ignore[arg-type]
    result = await get_snapshot(redis_client, "aic-003")  # type: ignore[arg-type]
    assert result is not None
    assert result.observed_at_ms == BASE_MS  # 保持原值，未回退


# ── mget_snapshots ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mget_preserves_order(redis_client: object) -> None:
    """mget_snapshots 返回顺序与入参 aics 一致。"""
    await seed_snapshot(redis_client, aic="aic-a", observed_at_ms=BASE_MS + 100)  # type: ignore[arg-type]
    await seed_snapshot(redis_client, aic="aic-b", observed_at_ms=BASE_MS + 200)  # type: ignore[arg-type]
    await seed_snapshot(redis_client, aic="aic-c", observed_at_ms=BASE_MS + 300)  # type: ignore[arg-type]

    results = await mget_snapshots(redis_client, ["aic-c", "aic-a", "aic-b"])  # type: ignore[arg-type]
    assert len(results) == 3
    assert results[0] is not None and results[0].aic == "aic-c"
    assert results[1] is not None and results[1].aic == "aic-a"
    assert results[2] is not None and results[2].aic == "aic-b"


@pytest.mark.asyncio
async def test_mget_returns_none_for_missing(redis_client: object) -> None:
    """不存在的 AIC 返回 None（保序）。"""
    results = await mget_snapshots(redis_client, ["ghost"])  # type: ignore[arg-type]
    assert results == [None]


# ── scan_index_desc ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scan_index_desc_order(redis_client: object) -> None:
    """scan_index_desc 返回 observedAt 降序。"""
    for i in range(3):
        await seed_snapshot(redis_client, aic=f"aic-scan-{i}", observed_at_ms=BASE_MS + i * 1000)  # type: ignore[arg-type]

    items = await scan_index_desc(redis_client, cursor=None, batch_size=10)  # type: ignore[arg-type]
    scores = [score for _, score in items]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_scan_index_desc_cursor_tie_break_by_aic(redis_client: object) -> None:
    """同 observedAt 时 cursor.aic 续读不重不漏（§6.12）。"""
    from app.metrics.cursor import SnapshotCursor
    from app.metrics.snapshot_cache import scan_index_desc

    ts = BASE_MS
    for aic in ("aic-a", "aic-m", "aic-z"):
        await seed_snapshot(redis_client, aic=aic, observed_at_ms=ts)  # type: ignore[arg-type]

    page1 = await scan_index_desc(redis_client, cursor=None, batch_size=2)  # type: ignore[arg-type]
    assert page1 == [("aic-a", ts), ("aic-m", ts)]

    cursor = SnapshotCursor(observed_at_ms=ts, aic="aic-m", fingerprint="fp")
    page2 = await scan_index_desc(redis_client, cursor=cursor, batch_size=10)  # type: ignore[arg-type]
    assert page2 == [("aic-z", ts)]


# ── remove_index_entry ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_remove_index_entry_removes_from_zset(redis_client: object) -> None:
    """remove_index_entry 清理 ZSet 悬挂索引项。"""
    await seed_snapshot(redis_client, aic="aic-rm", observed_at_ms=BASE_MS)  # type: ignore[arg-type]
    await remove_index_entry(redis_client, "aic-rm")  # type: ignore[arg-type]
    score = await redis_client.zscore(SNAPSHOT_INDEX_KEY, "aic-rm")  # type: ignore[attr-defined]
    assert score is None
