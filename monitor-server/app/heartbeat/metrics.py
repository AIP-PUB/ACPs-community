"""Heartbeat 模块 — 进程内指标注册表（§10.1 C-OBS-1/2）。

HeartbeatMetrics 是轻量内存注册表，支持：
- inc(name, value, shard=): 递增计数器
- gauge(name, value, shard=): 设置 gauge
- observe_ms(name, ms): 累加耗时 ms 到 <name>_ms_total
- snapshot(): 返回所有指标的快照 dict

module-level singleton:  metrics = HeartbeatMetrics()

异步工具：
- _sample_state_gauges(redis): 采样 Redis 当前态四个 gauge（P1-5）
- metrics_log_loop(interval_s): 周期输出全量指标快照（structlog kv 风格）
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

import structlog
from redis.asyncio import Redis

from app.core.config import settings
from app.heartbeat.sharding import all_shard_ids
from app.heartbeat.store import redis_now_ms, zcard, zcount_score_at_least

logger = structlog.get_logger(__name__)

# ── HeartbeatMetrics ─────────────────────────────────────────────────────────


class HeartbeatMetrics:
    """进程内指标注册表（内存 counter / gauge / ms 累计）。

    指标名严格采用 §10.1 清单，供 metrics_log_loop 周期输出及测试断言。
    Prometheus / OTLP 导出延后至 D-4 阶段，届时直接对齐本注册表键名。
    """

    def __init__(self) -> None:
        self._data: dict[str, int] = defaultdict(int)

    def _key(self, name: str, shard: str | None) -> str:
        if shard is not None:
            return f"{name}{{shard={shard}}}"
        return name

    def inc(self, name: str, value: int = 1, *, shard: str | None = None) -> None:
        """递增计数器。

        Args:
            name: 指标名（§10.1 清单）。
            value: 增量（默认 1）。
            shard: 可选分片标签；非空时键名为 'name{shard=<shard>}'。
        """
        self._data[self._key(name, shard)] += value

    def gauge(self, name: str, value: int, *, shard: str | None = None) -> None:
        """设置 gauge（覆盖）。

        Args:
            name: 指标名。
            value: 当前值（覆盖上次）。
            shard: 可选分片标签。
        """
        self._data[self._key(name, shard)] = value

    def observe_ms(self, name: str, ms: int) -> None:
        """累加耗时 ms 到 '<name>_ms_total'。

        Args:
            name: 耗时指标前缀（如 "snapshot_enum"）。
            ms: 本次观测 ms 数值。
        """
        self._data[f"{name}_ms_total"] += ms

    def snapshot(self) -> dict[str, int]:
        """返回所有指标当前值的快照 dict（独立 copy）。

        Returns:
            dict[str, int]: {指标键名 → 值}。
        """
        return dict(self._data)


metrics = HeartbeatMetrics()  # 模块级单例

# ── gauge 采样 ────────────────────────────────────────────────────────────────


async def _sample_state_gauges(redis: Redis) -> None:
    """采样 Redis 当前态四个 gauge（§10.1 P1-5 修复）。

    遍历 all_shard_ids()，分别调 store.zcard / store.zcount_score_at_least：
    - amp_heartbeat_latest_rows       = sum(ZCARD liveness_zset) over shards
    - amp_heartbeat_liveness_zset_size= 同上（本实现 zset 即 latest 集合，两者等价）
    - amp_heartbeat_alive_rows        = sum(ZCOUNT score >= now-silenceThresholdMs) over shards
    - amp_heartbeat_silent_rows       = latest_rows - alive_rows

    Redis 异常时仅打 WARNING，不中断 metrics_log_loop（不传播）。

    Args:
        redis: Redis 客户端。
    """
    try:
        now_ms = await redis_now_ms(redis)
        silence_ms = settings.heartbeat_silence_threshold_seconds * 1000
        min_alive_score = now_ms - silence_ms

        total_latest = 0
        total_alive = 0
        for shard in all_shard_ids():
            total_latest += await zcard(redis, shard)
            total_alive += await zcount_score_at_least(redis, shard, min_alive_score)

        total_silent = total_latest - total_alive
        metrics.gauge("amp_heartbeat_latest_rows", total_latest)
        metrics.gauge("amp_heartbeat_liveness_zset_size", total_latest)
        metrics.gauge("amp_heartbeat_alive_rows", total_alive)
        metrics.gauge("amp_heartbeat_silent_rows", total_silent)
    except Exception:
        logger.warning("_sample_state_gauges failed", exc_info=True)


# ── 周期日志循环 ──────────────────────────────────────────────────────────────


async def metrics_log_loop(interval_s: int) -> None:
    """周期 INFO 输出全量指标快照（structlog kv 风格，C-OBS-1）。

    每轮先调用 _sample_state_gauges 刷新 Redis 当前态 gauge，再输出全量 snapshot。
    被 HeartbeatRuntime.start() 作为后台 task 启动。

    Args:
        interval_s: 输出间隔秒数（对应 settings.heartbeat_metrics_log_interval_seconds）。
    """
    from app.core.redis_client import get_redis

    while True:
        try:
            await asyncio.sleep(interval_s)
            await _sample_state_gauges(get_redis())
            snap = metrics.snapshot()
            logger.info("heartbeat_metrics", **snap)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("metrics_log_loop iteration failed", exc_info=True)
