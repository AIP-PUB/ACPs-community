"""tests/integration/test_access_dedupe.py — Access Redis 去重集成测试（C-3）。

测试真实 Redis 下 dedupe.py / freshness.py / trace_hint.py 的行为。
需要真实 Redis（dev-infra redis 服务，db=1）。
"""

from __future__ import annotations

import time

import pytest
from redis.asyncio import Redis

from tests.support.constants import TEST_REDIS_URL
from tests.support.redis_helper import reset_access_redis_state


@pytest.fixture
async def redis_access():
    """提供连接测试库 db=1 的 Redis 客户端；前后清理 amp:access:* 键。"""
    r = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        await r.ping()
    except Exception as exc:
        await r.aclose()
        pytest.skip(f"Redis 不可达，跳过集成测试：{exc}")

    await reset_access_redis_state(r)
    yield r
    await reset_access_redis_state(r)
    await r.aclose()


# ── dedupe.py ─────────────────────────────────────────────────────────────────


class TestAccessDedupe:
    """dedupe.filter_unseen / mark_seen 真实 Redis 测试（C-3，fail-open 语义）。"""

    async def test_unseen_before_mark(self, redis_access: Redis) -> None:
        """新 log_id 在 mark 前 filter_unseen 应包含它。"""
        from app.access.dedupe import filter_unseen

        log_id = "dedup-int-001"
        unseen, available = await filter_unseen(redis_access, [log_id])
        assert available is True
        assert log_id in unseen

    async def test_seen_after_mark(self, redis_access: Redis) -> None:
        """mark_seen 后 filter_unseen 不再包含已标记 id。"""
        from app.access.dedupe import filter_unseen, mark_seen

        log_id = "dedup-int-002"
        await mark_seen(redis_access, [log_id], ttl_seconds=3600)
        unseen, available = await filter_unseen(redis_access, [log_id])
        assert available is True
        assert log_id not in unseen

    async def test_different_ids_independent(self, redis_access: Redis) -> None:
        """mark id-A 后 id-B 仍被视为 unseen。"""
        from app.access.dedupe import filter_unseen, mark_seen

        await mark_seen(redis_access, ["id-A"], ttl_seconds=3600)
        unseen, _ = await filter_unseen(redis_access, ["id-B"])
        assert "id-B" in unseen

    async def test_mark_idempotent(self, redis_access: Redis) -> None:
        """mark_seen 幂等：重复调用不报错，seen 状态不变。"""
        from app.access.dedupe import filter_unseen, mark_seen

        log_id = "dedup-int-idem"
        await mark_seen(redis_access, [log_id], ttl_seconds=3600)
        await mark_seen(redis_access, [log_id], ttl_seconds=3600)
        unseen, _ = await filter_unseen(redis_access, [log_id])
        assert log_id not in unseen

    async def test_redis_failure_is_fail_open(self) -> None:
        """Redis 不可达时 filter_unseen 返回全量 + available=False（fail-open）。"""
        from app.access.dedupe import filter_unseen

        bad_redis = Redis.from_url("redis://localhost:19999/99", decode_responses=True)
        try:
            unseen, available = await filter_unseen(bad_redis, ["any-id"])
            assert available is False
            assert "any-id" in unseen
        finally:
            await bad_redis.aclose()

    async def test_batch_partial_mark(self, redis_access: Redis) -> None:
        """批量：只 mark 部分 id 后，unseen 仅含未 mark 的那些。"""
        from app.access.dedupe import filter_unseen, mark_seen

        ids = ["batch-a", "batch-b", "batch-c"]
        await mark_seen(redis_access, ["batch-a", "batch-c"], ttl_seconds=3600)
        unseen, _ = await filter_unseen(redis_access, ids)
        assert "batch-b" in unseen
        assert "batch-a" not in unseen
        assert "batch-c" not in unseen


# ── freshness.py ──────────────────────────────────────────────────────────────


class TestAccessFreshness:
    """advance_partition_watermark / read_overall_watermark / evaluate_freshness 真实 Redis。"""

    async def test_advance_and_read_watermark(self, redis_access: Redis) -> None:
        """advance 后 read_overall_watermark 返回推进后的值。"""
        from app.access.freshness import advance_partition_watermark, read_overall_watermark

        now_ms = int(time.time() * 1000)
        await advance_partition_watermark(redis_access, partition_id=0, batch_max_ts_ms=now_ms, now_ms=now_ms)
        wm = await read_overall_watermark(redis_access)
        assert wm == now_ms

    async def test_advance_does_not_go_backward(self, redis_access: Redis) -> None:
        """水位单调不回退：老 batch 不应拉低已有水位。"""
        from app.access.freshness import advance_partition_watermark, read_overall_watermark

        now_ms = int(time.time() * 1000)
        await advance_partition_watermark(redis_access, partition_id=0, batch_max_ts_ms=now_ms, now_ms=now_ms)
        # 投递旧批次
        await advance_partition_watermark(redis_access, partition_id=0, batch_max_ts_ms=now_ms - 10_000, now_ms=now_ms)
        wm = await read_overall_watermark(redis_access)
        assert wm == now_ms

    async def test_overall_watermark_is_minimum_of_partitions(self, redis_access: Redis) -> None:
        """整体水位 = min(各分区水位)；慢分区不被快分区掩盖。"""
        from app.access.freshness import advance_partition_watermark, read_overall_watermark

        now_ms = int(time.time() * 1000)
        slow_wm = now_ms - 60_000  # 分区 0 落后 60s
        fast_wm = now_ms  # 分区 1 最新

        await advance_partition_watermark(redis_access, partition_id=0, batch_max_ts_ms=slow_wm, now_ms=now_ms)
        await advance_partition_watermark(redis_access, partition_id=1, batch_max_ts_ms=fast_wm, now_ms=now_ms)
        overall = await read_overall_watermark(redis_access)
        assert overall == slow_wm, "整体水位应等于最慢分区"

    async def test_evaluate_freshness_no_watermarks(self, redis_access: Redis) -> None:
        """无任何水位 → lagging=True，data_freshness_at_ms=None。"""
        from app.access.freshness import evaluate_freshness

        view = await evaluate_freshness(redis_access)
        assert view.lagging is True
        assert view.data_freshness_at_ms is None

    async def test_evaluate_freshness_fresh(self, redis_access: Redis) -> None:
        """所有分区水位刚更新 → is_fresh（lag < threshold）。"""
        from app.access.freshness import advance_partition_watermark, evaluate_freshness
        from app.core.config import settings

        now_ms = int(time.time() * 1000)
        for part in range(settings.access_partition_count if hasattr(settings, "access_partition_count") else 1):
            await advance_partition_watermark(redis_access, partition_id=part, batch_max_ts_ms=now_ms, now_ms=now_ms)
        view = await evaluate_freshness(redis_access, now_ms=now_ms)
        assert view.lagging is False

    async def test_advance_idle_partition(self, redis_access: Redis) -> None:
        """advance_idle_partition 把水位推到 now_ms。"""
        from app.access.freshness import advance_idle_partition, read_overall_watermark

        now_ms = int(time.time() * 1000)
        await advance_idle_partition(redis_access, partition_id=0, now_ms=now_ms)
        wm = await read_overall_watermark(redis_access)
        assert wm == now_ms


# ── trace_hint.py ─────────────────────────────────────────────────────────────


class TestAccessTraceHint:
    """mark_traces / maybe_seen 真实 Redis 测试（C-3）。"""

    async def test_mark_then_maybe_seen_returns_true(self, redis_access: Redis) -> None:
        """mark 后 maybe_seen 应返回 True。"""
        from app.access.trace_hint import mark_traces, maybe_seen

        trace_id = "tr-hint-001"
        await mark_traces(redis_access, {trace_id}, ttl_seconds=3600)
        seen = await maybe_seen(redis_access, trace_id)
        assert seen is True

    async def test_unknown_trace_returns_false(self, redis_access: Redis) -> None:
        """未 mark 的 trace_id → maybe_seen 返回 False。"""
        from app.access.trace_hint import maybe_seen

        seen = await maybe_seen(redis_access, "tr-nonexistent")
        assert seen is False

    async def test_mark_multiple_traces(self, redis_access: Redis) -> None:
        """批量 mark 多个 trace_id 后均可 maybe_seen=True。"""
        from app.access.trace_hint import mark_traces, maybe_seen

        ids = {"tr-a", "tr-b", "tr-c"}
        await mark_traces(redis_access, ids, ttl_seconds=3600)
        for tid in ids:
            assert await maybe_seen(redis_access, tid) is True

    async def test_mark_empty_set_is_noop(self, redis_access: Redis) -> None:
        """空集合 mark_traces 不报错。"""
        from app.access.trace_hint import mark_traces

        await mark_traces(redis_access, set(), ttl_seconds=3600)

    async def test_maybe_seen_redis_failure_returns_none(self) -> None:
        """Redis 不可达时 maybe_seen 返回 None（调用方应忽略预检直接查 CH）。"""
        from app.access.trace_hint import maybe_seen

        bad_redis = Redis.from_url("redis://localhost:19999/99", decode_responses=True)
        try:
            result = await maybe_seen(bad_redis, "any-trace")
            assert result is None
        finally:
            await bad_redis.aclose()
