"""app/access/dedupe.py — Writer 持久化去重窗口（Redis，fail-open）。

实现设计 §3.1，C-ACCESS-WRITE-3/5/7。
与 metrics 的 SET NX before write 取向不同：
access 必须满足三步提交「先写 CH 再写去重标记」，
故拆成「写前只读检查」+「写后标记」两步。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = structlog.get_logger(__name__)

KEY_PREFIX: Final = "amp:access:dedupe:"  # 键 = amp:access:dedupe:{log_id}


async def filter_unseen(redis: Redis, log_ids: list[str]) -> tuple[set[str], bool]:
    """写 CH 前的只读检查（不占位）。

    MGET 批量查 → 返回 (尚未见过的 log_id 集合, dedupe_available)。

    fail-open（C-ACCESS-WRITE-5）：Redis 异常 → 返回 (全部 log_ids, False)。
    调用方据 available=False 递增 amp_access_writer_dedup_unavailable_total，
    并照常写入（停写代价 > 少量重复）。
    """
    if not log_ids:
        return set(), True

    keys = [f"{KEY_PREFIX}{lid}" for lid in log_ids]
    try:
        values = await redis.mget(*keys)
        unseen = {lid for lid, v in zip(log_ids, values, strict=False) if v is None}
        return unseen, True
    except Exception:
        logger.warning("dedupe.filter_unseen: Redis error, failing open", exc_info=True)
        return set(log_ids), False


async def mark_seen(redis: Redis, log_ids: list[str], *, ttl_seconds: int) -> None:
    """写 CH 成功后调用（三步提交第 2 步）。

    Pipeline 批量 SET {key} 1 EX {ttl}。
    fail-open：异常只告警不抛（标记缺失只导致后续可能重复，不丢数）。
    """
    if not log_ids:
        return

    try:
        async with redis.pipeline(transaction=False) as pipe:
            for lid in log_ids:
                pipe.set(f"{KEY_PREFIX}{lid}", 1, ex=ttl_seconds)
            await pipe.execute()
    except Exception:
        logger.warning("dedupe.mark_seen: Redis error, marking skipped", exc_info=True)
