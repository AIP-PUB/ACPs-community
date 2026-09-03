"""app/metrics/dedupe.py — Writer 去重窗口（Redis SET NX + TTL）。

实现设计 §3.1 第 1 条、C-METRIC-WRITE-4：
在 Remote Write 之前用持久化去重窗口完成 log_id 去重。
"""

from __future__ import annotations

from redis.asyncio import Redis


async def claim_log_ids(redis: Redis, log_ids: list[str]) -> set[str]:
    """对一批 log_id 原子占用去重窗口（pipeline SET NX EX）。

    在 Remote Write **之前**调用，返回"本次新占用成功"的 log_id 集合。
    已存在（NX 失败）的视为重复投递，调用方从批次中剔除其样本（C-METRIC-WRITE-4）。

    Args:
        redis: Redis 客户端（decode_responses=True）。
        log_ids: 待占用的 log_id 列表。

    Returns:
        set[str]: 新占用成功的 log_id 集合（子集）。
    """
    if not log_ids:
        return set()

    from app.core.config import get_settings

    ttl = get_settings().metrics_dedupe_ttl_seconds

    async with redis.pipeline(transaction=False) as pipe:
        for log_id in log_ids:
            key = f"amp:metrics:dedupe:{log_id}"
            await pipe.set(key, "1", nx=True, ex=ttl)
        results = await pipe.execute()

    claimed: set[str] = set()
    for log_id, ok in zip(log_ids, results, strict=True):
        if ok:  # SET NX 成功（返回 True）
            claimed.add(log_id)
    return claimed


async def release_log_ids(redis: Redis, log_ids: list[str]) -> None:
    """回滚已占用的 log_id（Remote Write 最终失败时调用）。

    DEL 对应去重键，使后续重投递可再次占用（C-METRIC-WRITE-1 语义一致性）。
    回滚失败的极少数残留按"宁可漏写不可重复"容忍。

    Args:
        redis: Redis 客户端。
        log_ids: 需要回滚的 log_id 列表。
    """
    if not log_ids:
        return

    keys = [f"amp:metrics:dedupe:{log_id}" for log_id in log_ids]
    async with redis.pipeline(transaction=False) as pipe:
        for key in keys:
            await pipe.delete(key)
        await pipe.execute()


__all__ = [
    "claim_log_ids",
    "release_log_ids",
]
