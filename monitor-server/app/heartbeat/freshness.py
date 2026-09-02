"""Heartbeat 模块 — 读模型新鲜度评估（C-QUERY-5）。

职责：
1. 评估 writer watermark 的新鲜度
2. 计算读模型延迟并决定是否降级响应
3. 推算单点查询（GET /liveness/{aic}）对应的 Kafka 输入分区
"""

from __future__ import annotations

from dataclasses import dataclass, field

from redis.asyncio import Redis

from app.core.config import settings
from app.heartbeat.sharding import input_partition_for_aic
from app.heartbeat.store import read_watermarks


@dataclass(frozen=True)
class FreshnessView:
    """读模型新鲜度快照。"""

    min_watermark_ms: int
    """所有已知分区 watermark 的最小值（即最旧的数据时间）。"""

    lagging_partition_count: int
    """请求分区中缺失或过期的分区数（C-QUERY-5：不剔除，只计数）。"""

    all_unknown: bool
    """无任何 watermark 记录（Writer 从未写入或已重启）。"""

    watermarks: dict[int, int] = field(default_factory=dict)
    """已知分区 → watermark_ms 映射（供调用方拆解）。"""


async def evaluate_freshness(
    redis: Redis,
    *,
    partitions: list[int] | None,
    now_ms: int,
) -> FreshnessView:
    """评估读模型新鲜度（C-QUERY-5）。

    partitions=None 表示全局查询（不检查具体分区，lagging_count=0）。
    C-QUERY-5：缺失/过期分区计入 lagging，不从结果集剔除。

    Args:
        redis: Redis 客户端。
        partitions: 需要检查的 Kafka 输入分区列表；None = 全局查询。
        now_ms: 当前时间（epoch ms，由调用方传入，保持单调）。

    Returns:
        FreshnessView 新鲜度快照。
    """
    raw = await read_watermarks(redis)
    wm_map: dict[int, int] = {p: v[0] for p, v in raw.items()}

    if not wm_map:
        count = len(partitions) if partitions is not None else 0
        return FreshnessView(
            min_watermark_ms=now_ms,
            lagging_partition_count=count,
            all_unknown=True,
            watermarks={},
        )

    min_wm = min(wm_map.values())
    stale_ms = settings.heartbeat_writer_watermark_stale_after_ms

    if partitions is None:
        lagging = 0
    else:
        lagging = 0
        for p in partitions:
            if p not in wm_map or now_ms - wm_map[p] > stale_ms:
                lagging += 1

    return FreshnessView(
        min_watermark_ms=min_wm,
        lagging_partition_count=lagging,
        all_unknown=False,
        watermarks=wm_map,
    )


def point_lookup_partitions(aic: str) -> list[int]:
    """推算 AIC 心跳写入的 Kafka 输入分区（C-SHARD-4）。

    用于 GET /liveness/{aic} 的新鲜度检查：只需检查该 AIC 所在的分区。

    Args:
        aic: Agent Identity Code。

    Returns:
        长度为 1 的分区列表（Kafka DefaultPartitioner 路由规则）。
    """
    count = settings.heartbeat_input_partition_count
    p = input_partition_for_aic(aic, count)
    return [p]


def apply_degrade_policy(view: FreshnessView, *, strict_503: bool) -> bool:
    """决定是否降级响应（503 或 partial）。

    降级条件：
    - all_unknown（无 watermark）→ 必然降级
    - strict_503=True 且 lagging_partition_count > 0 → 降级

    strict_503=False 允许 partial 响应（lagging 分区用 null/placeholder 填充）。

    Args:
        view: 新鲜度快照。
        strict_503: True = 任意 lagging 触发降级；False = 容忍 partial。

    Returns:
        True = 需要降级；False = 可正常服务。
    """
    if view.all_unknown:
        return True
    return strict_503 and view.lagging_partition_count > 0
