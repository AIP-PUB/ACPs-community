"""tests/integration/test_heartbeat_reconciler_integration.py — Reconciler 集成测试（§9.3）。

需要 Redis 7+ 在 localhost:6379 可用（testing.toml db=3）。
运行：just test integration -k heartbeat_reconciler

测试配置（config/testing.toml）：
  silence_threshold_seconds = 2
  evict_after_seconds        = 6

Silent 候选窗口（exclusive）：(now-6000ms, now-2000ms)
Evict 候选窗口（inclusive）：-inf ~ now-6000ms
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator

import pytest

from app.core.config import settings
from app.heartbeat.functions import (
    mark_silent_one,
)
from app.heartbeat.reconciler import HeartbeatReconciler
from app.heartbeat.redis_keys import latest_key, liveness_zset_key
from app.heartbeat.sharding import shard_id_for_aic
from tests.support.redis_helper import (
    ensure_functions_for_tests,
    read_outbox,
    reset_heartbeat_redis_state,
    seed_heartbeat,
)

pytestmark = pytest.mark.integration

AIC = "recon-aic-001"
AIC2 = "recon-aic-002"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
async def redis_client():
    from app.core.redis_client import close_redis, get_redis

    r = get_redis()
    yield r
    await close_redis()


@pytest.fixture(autouse=True)
async def isolated_redis(redis_client: object) -> AsyncGenerator[None]:
    await reset_heartbeat_redis_state(redis_client)  # type: ignore[arg-type]
    yield
    await reset_heartbeat_redis_state(redis_client)  # type: ignore[arg-type]


@pytest.fixture(scope="session")
async def loaded_functions(redis_client: object) -> None:
    await ensure_functions_for_tests(redis_client)  # type: ignore[arg-type]


# ── Silent Phase ──────────────────────────────────────────────────────────────


class TestSilentPhase:
    async def test_silent_phase_transitions_expired_aic(self, redis_client, loaded_functions) -> None:
        """超过 silence_threshold 的 AIC 被 silent phase 转为 left_alive。

        testing.toml: silence=2s, evict=6s → 候选窗口 (now-6s, now-2s)
        此处 seed 3s 前的心跳（落在窗口内）。
        """
        shard = shard_id_for_aic(AIC)
        silence_ms = settings.heartbeat_silence_threshold_seconds * 1000
        # 3s 前：在 silent 窗口 (now-6000, now-2000) 内
        old_ms = int(time.time() * 1000) - silence_ms - 1_000
        await seed_heartbeat(redis_client, aic=AIC, observed_at_ms=old_ms)

        rec = HeartbeatReconciler(redis=redis_client)
        await rec._run_silent_phase(shard)

        assert rec._silent_transition == 1
        assert rec._silent_candidates == 1

    async def test_silent_phase_skips_fresh_aic(self, redis_client, loaded_functions) -> None:
        """最近有心跳的 AIC 不在 silent phase 候选范围内。"""
        shard = shard_id_for_aic(AIC)
        # 0.5s 前：高于 silence_threshold（2s），不在候选窗口
        fresh_ms = int(time.time() * 1000) - 500
        await seed_heartbeat(redis_client, aic=AIC, observed_at_ms=fresh_ms)

        rec = HeartbeatReconciler(redis=redis_client)
        await rec._run_silent_phase(shard)

        assert rec._silent_candidates == 0
        assert rec._silent_transition == 0

    async def test_silent_phase_skips_already_left_alive(self, redis_client, loaded_functions) -> None:
        """已经是 left_alive 状态的 AIC，mark_silent_one 返回 skipped_membership。

        mark_silent_one 正常流程会 ZREM AIC，需手动重新加入 ZSet 模拟 crash-recovery 场景。
        """
        shard = shard_id_for_aic(AIC)
        silence_ms = settings.heartbeat_silence_threshold_seconds * 1000
        old_ms = int(time.time() * 1000) - silence_ms - 1_000  # 3s 前，在窗口内

        await seed_heartbeat(redis_client, aic=AIC, observed_at_ms=old_ms)

        # 先手动触发一次 mark_silent_one → left_alive + ZREM
        await mark_silent_one(redis_client, shard=shard, aic=AIC)

        # 模拟 crash-recovery：重新加入 ZSet（hash 仍是 left_alive）
        await redis_client.zadd(liveness_zset_key(shard), {AIC: old_ms})

        rec = HeartbeatReconciler(redis=redis_client)
        await rec._run_silent_phase(shard)

        # hash 是 left_alive → skipped_membership
        assert rec._silent_skipped_race == 1

    async def test_silent_phase_race_skip_on_refresh(self, redis_client, loaded_functions) -> None:
        """ZSet score 在窗口内但 hash 已被新心跳刷新，mark_silent_one 应 skipped_refreshed。"""
        shard = shard_id_for_aic(AIC)
        silence_ms = settings.heartbeat_silence_threshold_seconds * 1000

        # 初始种一个在窗口内的 score（3s 前）
        old_ms = int(time.time() * 1000) - silence_ms - 1_000
        await seed_heartbeat(redis_client, aic=AIC, observed_at_ms=old_ms)

        # 模拟「并发新心跳」刷新 hash.last_seen_at_ms → 0.5s 前
        fresh_ms = int(time.time() * 1000) - 500
        await seed_heartbeat(redis_client, aic=AIC, observed_at_ms=fresh_ms)

        # 将 zset score 手动拉回 old_ms（ZSet 落后于 hash，模拟竞争）
        await redis_client.zadd(liveness_zset_key(shard), {AIC: old_ms})

        rec = HeartbeatReconciler(redis=redis_client)
        await rec._run_silent_phase(shard)

        # ZSet 候选进入（score 在窗口内），但 mark_silent_one 发现 hash 里是新时间 → skip
        assert rec._silent_skipped_race == 1


# ── Evict Phase ───────────────────────────────────────────────────────────────


class TestEvictPhase:
    async def test_evict_phase_removes_left_alive_aic(self, redis_client, loaded_functions) -> None:
        """left_alive 状态 AIC 仍在 ZSet（crash-recovery 场景）被 evict phase 干净删除。

        mark_silent_one 正常路径会 ZREM，故手动设置 left_alive + 重新加入 ZSet。
        """
        shard = shard_id_for_aic(AIC)
        evict_ms = settings.heartbeat_evict_after_seconds * 1000
        very_old_ms = int(time.time() * 1000) - evict_ms - 1_000  # 7s 前

        await seed_heartbeat(redis_client, aic=AIC, observed_at_ms=very_old_ms)

        # 手动设置 left_alive（不 ZREM，模拟 crash-recovery）
        await redis_client.hset(latest_key(shard, AIC), "alive_membership_state", "left_alive")
        # ZSet score 保持 very_old_ms（evict 阈值内）

        rec = HeartbeatReconciler(redis=redis_client)
        await rec._run_evict_phase(shard)

        assert rec._evict_gc == 1
        assert rec._evict_repair == 0  # left_alive 干净删除，不产出 delta
        assert await redis_client.hgetall(latest_key(shard, AIC)) == {}

    async def test_evict_phase_repair_path_when_still_alive(self, redis_client, loaded_functions) -> None:
        """跳过 silent phase 直接超 evict 阈值的 AIC（alive 状态），走 evict_repair 路径。"""
        shard = shard_id_for_aic(AIC)
        evict_ms = settings.heartbeat_evict_after_seconds * 1000
        very_old_ms = int(time.time() * 1000) - evict_ms - 1_000  # 7s 前

        await seed_heartbeat(redis_client, aic=AIC, observed_at_ms=very_old_ms)

        rec = HeartbeatReconciler(redis=redis_client)
        await rec._run_evict_phase(shard)

        # evicted_with_repair：同时计入 gc 和 repair
        assert rec._evict_gc == 1
        assert rec._evict_repair == 1

    async def test_evict_phase_skips_fresh_aic(self, redis_client, loaded_functions) -> None:
        """最近活跃的 AIC 不在 evict 候选范围内。"""
        shard = shard_id_for_aic(AIC)
        fresh_ms = int(time.time() * 1000) - 500
        await seed_heartbeat(redis_client, aic=AIC, observed_at_ms=fresh_ms)

        rec = HeartbeatReconciler(redis=redis_client)
        await rec._run_evict_phase(shard)

        assert rec._evict_candidates == 0
        assert rec._evict_gc == 0

    async def test_evict_phase_produces_repair_delta_in_outbox(self, redis_client, loaded_functions) -> None:
        """evict_repair 路径在 outbox 中产生 leave_alive delta 条目。"""
        shard = shard_id_for_aic(AIC)
        evict_ms = settings.heartbeat_evict_after_seconds * 1000
        very_old_ms = int(time.time() * 1000) - evict_ms - 1_000  # 7s 前

        await seed_heartbeat(redis_client, aic=AIC, observed_at_ms=very_old_ms)

        rec = HeartbeatReconciler(redis=redis_client)
        await rec._run_evict_phase(shard)

        outbox = await read_outbox(redis_client, shard)
        # evict_repair 产出的 delta：kind="leave_alive", op="delete", reason="evict_repair"
        assert len(outbox) >= 1
        assert any(e.get("kind") == "leave_alive" for e in outbox)


# ── run_one_shard 端到端 ──────────────────────────────────────────────────────


class TestRunOneShard:
    async def test_run_one_shard_runs_silent_phase_on_expired_aic(self, redis_client, loaded_functions) -> None:
        """run_one_shard 成功获取锁后，对超过 silence_threshold 的 AIC 完成 silent 转换。"""
        shard = shard_id_for_aic(AIC)
        silence_ms = settings.heartbeat_silence_threshold_seconds * 1000
        silent_aic_ms = int(time.time() * 1000) - silence_ms - 1_000  # 3s 前（窗口内）
        await seed_heartbeat(redis_client, aic=AIC, observed_at_ms=silent_aic_ms)

        rec = HeartbeatReconciler(redis=redis_client)
        await rec.run_one_shard(shard)

        assert rec._silent_transition >= 1

    async def test_run_one_shard_skips_when_lock_held_by_other(self, redis_client, loaded_functions) -> None:
        """另一实例持有锁时，run_one_shard 不调用任何 Function（C-RECON-1）。"""
        shard = shard_id_for_aic(AIC)
        silence_ms = settings.heartbeat_silence_threshold_seconds * 1000
        old_ms = int(time.time() * 1000) - silence_ms - 1_000
        await seed_heartbeat(redis_client, aic=AIC, observed_at_ms=old_ms)

        from app.heartbeat.redis_keys import scan_lock_key

        # 由另一节点持有锁
        await redis_client.set(scan_lock_key(shard), "other-node", ex=60)

        rec = HeartbeatReconciler(redis=redis_client)
        await rec.run_one_shard(shard)

        assert rec._silent_transition == 0
        assert rec._evict_gc == 0
