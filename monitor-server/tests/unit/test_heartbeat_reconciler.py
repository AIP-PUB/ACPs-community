"""HeartbeatReconciler 单元测试（Step 6 TDD 红阶段）。

覆盖：
- silent phase：扫描 ZSet 候选 → 逐 AIC 调用 mark_silent_one
- evict phase：扫描 ZSet 候选 → 逐 AIC 调用 evict_one
- 扫描锁获取 / 未获锁跳过
- 候选候选满 batch 时指标递增
- 合并 reconciler：先 evict phase（按间隔）再 silent phase
- 每 AIC 独立调用（C-RECON-2）
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── 常量 ───────────────────────────────────────────────────────────────────

SHARD = "hb-000"
BASE_MS = 1_700_000_000_000


# ── 辅助 mock ──────────────────────────────────────────────────────────────


def _make_reconcile_result(status: str, seq: int | None = None) -> MagicMock:
    r = MagicMock()
    r.status = status
    r.seq = seq
    return r


# ── Tests ──────────────────────────────────────────────────────────────────


class TestAcquireLock:
    """扫描锁获取（SET NX EX，C-RECON-1）。"""

    @pytest.mark.asyncio
    async def test_returns_true_when_lock_acquired(self) -> None:
        """Redis SET NX EX 返回 True 时，_acquire_lock 应返回 True。"""
        from app.heartbeat.reconciler import HeartbeatReconciler

        redis = AsyncMock()
        redis.set.return_value = True
        rec = HeartbeatReconciler.__new__(HeartbeatReconciler)
        rec._redis = redis
        rec._node_id = "test-node"

        result = await rec._acquire_lock(SHARD)
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_lock_not_acquired(self) -> None:
        """Redis SET NX EX 返回 None 时，_acquire_lock 应返回 False。"""
        from app.heartbeat.reconciler import HeartbeatReconciler

        redis = AsyncMock()
        redis.set.return_value = None
        rec = HeartbeatReconciler.__new__(HeartbeatReconciler)
        rec._redis = redis
        rec._node_id = "test-node"

        result = await rec._acquire_lock(SHARD)
        assert result is False

    @pytest.mark.asyncio
    async def test_lock_uses_nx_ex(self) -> None:
        """_acquire_lock 必须以 nx=True、ex=ttl 调用 Redis SET。"""
        from app.core.config import settings
        from app.heartbeat.reconciler import HeartbeatReconciler

        redis = AsyncMock()
        redis.set.return_value = True
        rec = HeartbeatReconciler.__new__(HeartbeatReconciler)
        rec._redis = redis
        rec._node_id = "node-1"

        await rec._acquire_lock(SHARD)

        redis.set.assert_called_once()
        call_kwargs = redis.set.call_args.kwargs
        assert call_kwargs.get("nx") is True
        assert call_kwargs.get("ex") == settings.heartbeat_scan_lock_ttl_seconds


class TestSilentPhase:
    """silent phase 扫描（C-RECON-2：每 AIC 独立调用 mark_silent_one）。

    B-1/B-2 修复后：mock store.zrange_by_score 和 store.redis_now_ms 替代 redis.zrangebyscore。
    """

    @pytest.mark.asyncio
    async def test_runs_silent_phase_for_each_candidate(self) -> None:
        """每个候选 AIC 应调用一次 mark_silent_one。"""
        from app.heartbeat.reconciler import HeartbeatReconciler

        redis = AsyncMock()
        mark_mock = AsyncMock(return_value=_make_reconcile_result("left_alive", 1))
        rec = HeartbeatReconciler.__new__(HeartbeatReconciler)
        rec._redis = redis
        rec._silent_transition = 0
        rec._silent_candidates = 0
        rec._silent_skipped_race = 0

        candidates = ["aic-001", "aic-002", "aic-003"]
        with (
            patch("app.heartbeat.reconciler.redis_now_ms", AsyncMock(return_value=BASE_MS)),
            patch(
                "app.heartbeat.reconciler.zrange_by_score",
                AsyncMock(return_value=[(aic, 0) for aic in candidates]),
            ),
            patch("app.heartbeat.reconciler.mark_silent_one", mark_mock),
        ):
            await rec._run_silent_phase(SHARD)

        assert mark_mock.call_count == 3
        for aic in candidates:
            mark_mock.assert_any_call(redis, shard=SHARD, aic=aic)

    @pytest.mark.asyncio
    async def test_increments_transition_count_on_left_alive(self) -> None:
        """mark_silent_one 返回 left_alive 时，_silent_transition 应加 1。"""
        from app.heartbeat.reconciler import HeartbeatReconciler

        redis = AsyncMock()
        mark_mock = AsyncMock(return_value=_make_reconcile_result("left_alive", 1))
        rec = HeartbeatReconciler.__new__(HeartbeatReconciler)
        rec._redis = redis
        rec._silent_transition = 0
        rec._silent_candidates = 0
        rec._silent_skipped_race = 0

        with (
            patch("app.heartbeat.reconciler.redis_now_ms", AsyncMock(return_value=BASE_MS)),
            patch(
                "app.heartbeat.reconciler.zrange_by_score",
                AsyncMock(return_value=[("aic-001", 0)]),
            ),
            patch("app.heartbeat.reconciler.mark_silent_one", mark_mock),
        ):
            await rec._run_silent_phase(SHARD)

        assert rec._silent_transition == 1

    @pytest.mark.asyncio
    async def test_increments_skipped_race_on_race(self) -> None:
        """mark_silent_one 返回非 left_alive 时，_silent_skipped_race 应加 1。"""
        from app.heartbeat.reconciler import HeartbeatReconciler

        redis = AsyncMock()
        mark_mock = AsyncMock(return_value=_make_reconcile_result("skipped_refreshed", None))
        rec = HeartbeatReconciler.__new__(HeartbeatReconciler)
        rec._redis = redis
        rec._silent_transition = 0
        rec._silent_candidates = 0
        rec._silent_skipped_race = 0

        with (
            patch("app.heartbeat.reconciler.redis_now_ms", AsyncMock(return_value=BASE_MS)),
            patch(
                "app.heartbeat.reconciler.zrange_by_score",
                AsyncMock(return_value=[("aic-001", 0)]),
            ),
            patch("app.heartbeat.reconciler.mark_silent_one", mark_mock),
        ):
            await rec._run_silent_phase(SHARD)

        assert rec._silent_skipped_race == 1

    @pytest.mark.asyncio
    async def test_empty_candidates_makes_no_calls(self) -> None:
        """候选列表为空时，mark_silent_one 不被调用。"""
        from app.heartbeat.reconciler import HeartbeatReconciler

        redis = AsyncMock()
        mark_mock = AsyncMock()
        rec = HeartbeatReconciler.__new__(HeartbeatReconciler)
        rec._redis = redis
        rec._silent_transition = 0
        rec._silent_candidates = 0
        rec._silent_skipped_race = 0

        with (
            patch("app.heartbeat.reconciler.redis_now_ms", AsyncMock(return_value=BASE_MS)),
            patch("app.heartbeat.reconciler.zrange_by_score", AsyncMock(return_value=[])),
            patch("app.heartbeat.reconciler.mark_silent_one", mark_mock),
        ):
            await rec._run_silent_phase(SHARD)

        mark_mock.assert_not_awaited()


class TestEvictPhase:
    """evict phase 扫描（C-RECON-2：每 AIC 独立调用 evict_one）。

    B-1/B-2 修复后：mock store.zrange_by_score 和 store.redis_now_ms 替代 redis.zrangebyscore。
    """

    @pytest.mark.asyncio
    async def test_runs_evict_phase_for_each_candidate(self) -> None:
        """每个候选 AIC 应调用一次 evict_one。"""
        from app.heartbeat.reconciler import HeartbeatReconciler

        redis = AsyncMock()
        evict_mock = AsyncMock(return_value=_make_reconcile_result("evicted", None))
        rec = HeartbeatReconciler.__new__(HeartbeatReconciler)
        rec._redis = redis
        rec._evict_gc = 0
        rec._evict_repair = 0
        rec._evict_candidates = 0
        rec._evict_skipped_race = 0

        candidates = ["aic-old-001", "aic-old-002"]
        with (
            patch("app.heartbeat.reconciler.redis_now_ms", AsyncMock(return_value=BASE_MS)),
            patch(
                "app.heartbeat.reconciler.zrange_by_score",
                AsyncMock(return_value=[(aic, 0) for aic in candidates]),
            ),
            patch("app.heartbeat.reconciler.evict_one", evict_mock),
        ):
            await rec._run_evict_phase(SHARD)

        assert evict_mock.call_count == 2

    @pytest.mark.asyncio
    async def test_increments_evict_gc_on_evicted(self) -> None:
        """evict_one 返回 evicted 时，_evict_gc 应加 1。"""
        from app.heartbeat.reconciler import HeartbeatReconciler

        redis = AsyncMock()
        evict_mock = AsyncMock(return_value=_make_reconcile_result("evicted", None))
        rec = HeartbeatReconciler.__new__(HeartbeatReconciler)
        rec._redis = redis
        rec._evict_gc = 0
        rec._evict_repair = 0
        rec._evict_candidates = 0
        rec._evict_skipped_race = 0

        with (
            patch("app.heartbeat.reconciler.redis_now_ms", AsyncMock(return_value=BASE_MS)),
            patch(
                "app.heartbeat.reconciler.zrange_by_score",
                AsyncMock(return_value=[("aic-001", 0)]),
            ),
            patch("app.heartbeat.reconciler.evict_one", evict_mock),
        ):
            await rec._run_evict_phase(SHARD)

        assert rec._evict_gc == 1

    @pytest.mark.asyncio
    async def test_increments_evict_repair_on_evicted_with_repair(self) -> None:
        """evict_one 返回 evicted_with_repair 时，_evict_repair 应加 1。"""
        from app.heartbeat.reconciler import HeartbeatReconciler

        redis = AsyncMock()
        evict_mock = AsyncMock(return_value=_make_reconcile_result("evicted_with_repair", 5))
        rec = HeartbeatReconciler.__new__(HeartbeatReconciler)
        rec._redis = redis
        rec._evict_gc = 0
        rec._evict_repair = 0
        rec._evict_candidates = 0
        rec._evict_skipped_race = 0

        with (
            patch("app.heartbeat.reconciler.redis_now_ms", AsyncMock(return_value=BASE_MS)),
            patch(
                "app.heartbeat.reconciler.zrange_by_score",
                AsyncMock(return_value=[("aic-001", 0)]),
            ),
            patch("app.heartbeat.reconciler.evict_one", evict_mock),
        ):
            await rec._run_evict_phase(SHARD)

        assert rec._evict_repair == 1
        assert rec._evict_gc == 1  # evict_with_repair 也算 GC 一条

    @pytest.mark.asyncio
    async def test_empty_candidates_makes_no_evict_calls(self) -> None:
        """候选列表为空时，evict_one 不被调用。"""
        from app.heartbeat.reconciler import HeartbeatReconciler

        redis = AsyncMock()
        evict_mock = AsyncMock()
        rec = HeartbeatReconciler.__new__(HeartbeatReconciler)
        rec._redis = redis
        rec._evict_gc = 0
        rec._evict_repair = 0
        rec._evict_candidates = 0
        rec._evict_skipped_race = 0

        with (
            patch("app.heartbeat.reconciler.redis_now_ms", AsyncMock(return_value=BASE_MS)),
            patch("app.heartbeat.reconciler.zrange_by_score", AsyncMock(return_value=[])),
            patch("app.heartbeat.reconciler.evict_one", evict_mock),
        ):
            await rec._run_evict_phase(SHARD)

        evict_mock.assert_not_awaited()


class TestRunOneShard:
    """run_one_shard：单次 shard 扫描（先 evict，再 silent，需持锁）。"""

    @pytest.mark.asyncio
    async def test_skips_when_lock_not_acquired(self) -> None:
        """未获取锁时，应跳过整个 shard 扫描（C-RECON-1）。"""
        from app.heartbeat.reconciler import HeartbeatReconciler

        rec = HeartbeatReconciler.__new__(HeartbeatReconciler)
        rec._redis = AsyncMock()
        rec._node_id = "n"
        rec._last_evict_at = {}

        with (
            patch.object(rec, "_acquire_lock", AsyncMock(return_value=False)),
            patch.object(rec, "_run_evict_phase", AsyncMock()) as evict_mock,
            patch.object(rec, "_run_silent_phase", AsyncMock()) as silent_mock,
        ):
            await rec.run_one_shard(SHARD)

        evict_mock.assert_not_awaited()
        silent_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_runs_evict_then_silent_when_evict_interval_elapsed(self) -> None:
        """evict 间隔已过时，先 evict 再 silent。"""
        from app.heartbeat.reconciler import HeartbeatReconciler

        rec = HeartbeatReconciler.__new__(HeartbeatReconciler)
        rec._redis = AsyncMock()
        rec._node_id = "n"
        rec._last_evict_at = {SHARD: 0.0}  # 从未执行，必然触发

        call_order: list[str] = []

        async def _fake_evict(shard: str) -> None:
            call_order.append("evict")

        async def _fake_silent(shard: str) -> None:
            call_order.append("silent")

        with (
            patch.object(rec, "_acquire_lock", AsyncMock(return_value=True)),
            patch.object(rec, "_run_evict_phase", _fake_evict),
            patch.object(rec, "_run_silent_phase", _fake_silent),
        ):
            await rec.run_one_shard(SHARD)

        assert call_order == ["evict", "silent"]

    @pytest.mark.asyncio
    async def test_skips_evict_when_interval_not_elapsed(self) -> None:
        """evict 间隔未过时，只执行 silent phase。"""
        from app.heartbeat.reconciler import HeartbeatReconciler

        rec = HeartbeatReconciler.__new__(HeartbeatReconciler)
        rec._redis = AsyncMock()
        rec._node_id = "n"
        rec._last_evict_at = {SHARD: time.monotonic()}  # 刚执行

        call_order: list[str] = []

        async def _fake_evict(shard: str) -> None:
            call_order.append("evict")

        async def _fake_silent(shard: str) -> None:
            call_order.append("silent")

        with (
            patch.object(rec, "_acquire_lock", AsyncMock(return_value=True)),
            patch.object(rec, "_run_evict_phase", _fake_evict),
            patch.object(rec, "_run_silent_phase", _fake_silent),
        ):
            await rec.run_one_shard(SHARD)

        assert "evict" not in call_order
        assert "silent" in call_order


class TestHeartbeatReconcilerInit:
    """HeartbeatReconciler 初始化。"""

    def test_init_counters_zeroed(self) -> None:
        """初始化后所有计数器应为 0。"""
        from app.heartbeat.reconciler import HeartbeatReconciler

        rec = HeartbeatReconciler(redis=AsyncMock())
        assert rec._silent_transition == 0
        assert rec._evict_gc == 0
        assert rec._evict_repair == 0
        assert rec._silent_candidates == 0
        assert rec._evict_candidates == 0

    def test_init_has_node_id(self) -> None:
        """初始化后应有 node_id（用于 scan_lock value）。"""
        from app.heartbeat.reconciler import HeartbeatReconciler

        rec = HeartbeatReconciler(redis=AsyncMock())
        assert rec._node_id
        assert isinstance(rec._node_id, str)
