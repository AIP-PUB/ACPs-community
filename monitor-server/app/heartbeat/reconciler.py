"""Heartbeat Reconciler — 双阶段后台任务（silent + evict，C-RECON-1/2）。

职责：
1. Silent phase：扫描 liveness_zset 中超过 silence_threshold 但未超 evict_after 的 AIC，
   逐一调用 hb_mark_silent_one 将其标记为 left_alive（§5.2）
2. Evict phase：扫描超过 evict_after 的 AIC，逐一调用 hb_evict_one 清除 Redis 记录（§5.3）
3. 分布式扫描锁（SET NX EX）防止多实例并发扫描同一 shard（C-RECON-1）
4. Evict phase 按独立时间间隔触发，默认 30s；silent phase 按 5s 触发

C-RECON-2：每 AIC 独立 for 循环调用（非 pipeline 批量），使 Lua Function 对每条记录原子判断。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from dataclasses import dataclass

import structlog
from redis.asyncio import Redis

from app.core.config import settings
from app.heartbeat.functions import evict_one, mark_silent_one
from app.heartbeat.metrics import metrics
from app.heartbeat.redis_keys import scan_lock_key
from app.heartbeat.sharding import all_shard_ids
from app.heartbeat.store import redis_now_ms, zrange_by_score

logger = structlog.get_logger(__name__)


@dataclass
class PhaseStats:
    """单次 phase 扫描统计（日志与指标用）。

    C-2 修复：字段名与设计对齐（silenced / evicted / repaired 替代 transitions）。
    """

    phase: str
    shard: str
    candidates: int = 0
    silenced: int = 0  # silent phase：left_alive 转换数（本轮增量）
    evicted: int = 0  # evict phase：本轮 evict 总数（gc + repair）
    repaired: int = 0  # evict phase：本轮 evicted_with_repair 数
    skipped_race: int = 0
    elapsed_ms: float = 0.0


class HeartbeatReconciler:
    """Heartbeat 双阶段后台 Reconciler（§3.2）。

    实例由 HeartbeatRuntime 创建并持有；Redis 客户端由调用方注入，
    Reconciler 不拥有其生命周期。

    Args:
        redis: 已初始化的 Redis 异步客户端。
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._node_id: str = str(uuid.uuid4())
        self._stop_flag: bool = False
        self._task: asyncio.Task[None] | None = None

        # 每 shard 上次 evict 执行时间（monotonic）
        self._last_evict_at: dict[str, float] = {}

        # 指标计数器（累计生命周期值）
        self._silent_transition: int = 0
        self._silent_candidates: int = 0
        self._silent_skipped_race: int = 0
        self._evict_gc: int = 0
        self._evict_repair: int = 0
        self._evict_candidates: int = 0
        self._evict_skipped_race: int = 0

    # ── 分布式扫描锁 ────────────────────────────────────────────────────────

    async def _acquire_lock(self, shard: str) -> bool:
        """尝试获取 shard 扫描锁（SET NX EX，C-RECON-1）。

        已持有（当前 node 是 owner）时用 SET XX EX 续期（P2-10）。

        Args:
            shard: 目标分片 id。

        Returns:
            True = 成功持有锁；False = 其他实例持有锁，跳过本轮。
        """
        key = scan_lock_key(shard)
        ttl = settings.heartbeat_scan_lock_ttl_seconds

        # 尝试新建锁
        result = await self._redis.set(key, self._node_id, nx=True, ex=ttl)
        if result is True:
            return True

        # 已有锁：检查是否为自己持有（续期）
        current_owner = await self._redis.get(key)
        if current_owner == self._node_id:
            renewed = await self._redis.set(key, self._node_id, xx=True, ex=ttl)
            return renewed is True

        return False

    # ── Silent Phase ─────────────────────────────────────────────────────────

    async def _run_silent_phase(self, shard: str) -> None:
        """扫描并静默超阈值 AIC（silent phase，C-RECON-2）。

        候选窗口：last_seen_at_ms ∈ (now-evictAfterMs, now-silenceThresholdMs)
        即：已过 silence_threshold 但未过 evict_after（P2-9：下界非 -inf）。

        B-1/B-2 修复：通过 store.zrange_by_score + store.redis_now_ms 替代
        直接 redis.zrangebyscore / time.time()（C-TIME-3 / store 抽象层）。

        每个候选独立调用 mark_silent_one（C-RECON-2）。

        Args:
            shard: 目标分片 id。
        """
        t0 = time.monotonic()
        now_ms = await redis_now_ms(self._redis)  # B-2: Redis TIME（C-TIME-3）
        silence_threshold_ms = settings.heartbeat_silence_threshold_seconds * 1000
        evict_after_ms = settings.heartbeat_evict_after_seconds * 1000

        # 排他下界：> now-evictAfterMs（不含已超 evict 阈值的 AIC）
        lower = f"({now_ms - evict_after_ms}"
        # 排他上界：< now-silenceThresholdMs（不含刚好在阈值点的 AIC）
        upper = f"({now_ms - silence_threshold_ms}"

        # B-1: 通过 store.zrange_by_score 替代直接 redis.zrangebyscore（store 抽象层）
        _results = await zrange_by_score(
            self._redis,
            shard,
            lower=lower,
            upper=upper,
            limit=settings.heartbeat_scan_batch_size,
            with_scores=False,
        )
        candidates = [aic for aic, _ in _results]

        self._silent_candidates += len(candidates)

        # B-5: 使用本轮增量计数（而非累计值）写入 PhaseStats 日志
        round_silenced = 0
        round_skipped_race = 0

        for aic in candidates:
            result = await mark_silent_one(self._redis, shard=shard, aic=aic)
            if result.status == "left_alive":
                self._silent_transition += 1
                round_silenced += 1
                metrics.inc("amp_heartbeat_reconciler_silenced_total")  # B-6
            else:
                self._silent_skipped_race += 1
                round_skipped_race += 1

        elapsed_ms = (time.monotonic() - t0) * 1000
        stats = PhaseStats(
            phase="silent",
            shard=shard,
            candidates=len(candidates),
            silenced=round_silenced,  # C-2: 本轮增量，命名对齐设计
            skipped_race=round_skipped_race,
            elapsed_ms=elapsed_ms,
        )
        logger.info(
            "Reconciler silent phase 完成",
            shard=stats.shard,
            candidates=stats.candidates,
            silenced=stats.silenced,
            skipped_race=stats.skipped_race,
            elapsed_ms=round(stats.elapsed_ms, 1),
        )

    # ── Evict Phase ──────────────────────────────────────────────────────────

    async def _run_evict_phase(self, shard: str) -> None:
        """扫描并驱逐超阈值 AIC（evict phase，C-RECON-2）。

        候选窗口：last_seen_at_ms <= now-evictAfterMs（即：已过 evict_after 阈值）。
        重启后首轮用 -inf 下界（_last_evict_at 为空时默认触发此逻辑）。

        B-1/B-2 修复：通过 store 层替代直接 Redis 调用（同 silent phase）。

        每个候选独立调用 evict_one（C-RECON-2）。

        Args:
            shard: 目标分片 id。
        """
        t0 = time.monotonic()
        now_ms = await redis_now_ms(self._redis)  # B-2: Redis TIME（C-TIME-3）
        evict_after_ms = settings.heartbeat_evict_after_seconds * 1000

        lower = "-inf"
        upper = str(now_ms - evict_after_ms)

        # B-1: 通过 store.zrange_by_score 替代直接 redis.zrangebyscore
        _results = await zrange_by_score(
            self._redis,
            shard,
            lower=lower,
            upper=upper,
            limit=settings.heartbeat_scan_batch_size,
            with_scores=False,
        )
        candidates = [aic for aic, _ in _results]

        self._evict_candidates += len(candidates)

        # B-5: 使用本轮增量计数（而非累计值）写入 PhaseStats 日志
        round_evicted = 0
        round_repaired = 0
        round_skipped_race = 0

        for aic in candidates:
            result = await evict_one(self._redis, shard=shard, aic=aic)
            if result.status in ("evicted", "evicted_with_repair"):
                self._evict_gc += 1
                round_evicted += 1
                metrics.inc("amp_heartbeat_reconciler_evicted_total")  # B-6
            if result.status == "evicted_with_repair":
                self._evict_repair += 1
                round_repaired += 1
                metrics.inc("amp_heartbeat_reconciler_repaired_total")  # B-6
            if result.status not in ("evicted", "evicted_with_repair"):
                self._evict_skipped_race += 1
                round_skipped_race += 1

        elapsed_ms = (time.monotonic() - t0) * 1000
        stats = PhaseStats(
            phase="evict",
            shard=shard,
            candidates=len(candidates),
            evicted=round_evicted,  # C-2: 本轮增量，命名对齐设计
            repaired=round_repaired,  # C-2: 新增 repaired 字段
            skipped_race=round_skipped_race,
            elapsed_ms=elapsed_ms,
        )
        logger.info(
            "Reconciler evict phase 完成",
            shard=stats.shard,
            candidates=stats.candidates,
            evicted=stats.evicted,
            repaired=stats.repaired,
            skipped_race=stats.skipped_race,
            elapsed_ms=round(stats.elapsed_ms, 1),
        )

    # ── 调度逻辑 ──────────────────────────────────────────────────────────────

    def _evict_due(self, shard: str) -> bool:
        """判断该 shard 是否到期触发 evict phase。

        Args:
            shard: 分片 id。

        Returns:
            True 表示已过 evict_scan_interval；首次（未记录）时始终返回 True。
        """
        last = self._last_evict_at.get(shard, 0.0)
        return (time.monotonic() - last) >= settings.heartbeat_evict_scan_interval_seconds

    async def run_one_shard(self, shard: str) -> None:
        """对单个 shard 执行一次 reconcile（获锁 → evict（按间隔） → silent）。

        Args:
            shard: 分片 id。
        """
        if not await self._acquire_lock(shard):
            logger.debug("跳过 shard reconcile（未获取扫描锁）", shard=shard)
            return

        if self._evict_due(shard):
            await self._run_evict_phase(shard)
            self._last_evict_at[shard] = time.monotonic()

        await self._run_silent_phase(shard)

    # ── 主循环 ───────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Reconciler 主循环：遍历所有 shard，按间隔 sleep（供 create_task 使用）。

        循环直到 stop() 调用。
        """
        logger.info("HeartbeatReconciler 启动", node_id=self._node_id)
        while not self._stop_flag:
            for shard in all_shard_ids():
                if self._stop_flag:
                    break
                await run_one_shard_safe(self, shard)
            await asyncio.sleep(settings.heartbeat_silent_scan_interval_seconds)
        logger.info("HeartbeatReconciler 停止", node_id=self._node_id)

    async def stop(self) -> None:
        """停止 Reconciler 主循环，取消后台 task（幂等）。

        不向调用方抛 CancelledError（C-RECON-7）。
        """
        self._stop_flag = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("HeartbeatReconciler 已请求停止")


async def run_one_shard_safe(rec: HeartbeatReconciler, shard: str) -> None:
    """异常安全的 run_one_shard 包装（不让单 shard 错误终止整个循环）。"""
    try:
        await rec.run_one_shard(shard)
    except Exception:
        logger.exception("Reconciler shard 处理异常，跳过本轮", shard=shard)
