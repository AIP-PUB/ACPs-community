"""app/access/trace_hint.py — 可选 trace_seen hint cache（设计 §4.4，默认关闭）。

实现设计 §4.4，C-ACCESS-MODEL-5。Redis Set 维护近期 trace 提示，
仅作性能预检，不决定 404。

正确性边界（C-ACCESS-MODEL-5）：
  maybe_seen 返回 False 不能直接作为 404 依据；
  最终 AMP_TRACE_NOT_FOUND 必须以 ClickHouse 查询结果为准。
  hint 只用于「可能不存在时省一次 CH 查询」的乐观优化，且本轮默认关闭。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = structlog.get_logger(__name__)

KEY: Final = "amp:access:trace_seen"


async def mark_traces(redis: Redis, trace_ids: set[str], *, ttl_seconds: int) -> None:
    """Writer flush 成功后可选调用：SADD 近期 trace_id（带过期）。

    失败只告警（不影响主表正确性，C-ACCESS-WRITE-4）。
    """
    if not trace_ids:
        return
    try:
        await redis.sadd(KEY, *trace_ids)
        await redis.expire(KEY, ttl_seconds)
    except Exception:
        logger.warning("trace_hint.mark_traces: Redis error, hint not updated", exc_info=True)


async def maybe_seen(redis: Redis, trace_id: str) -> bool | None:
    """traces/{traceId} 前置预检：SISMEMBER 查 hint。

    返回：
      True  — hint 显示见过（但不能作为 404 依据）
      False — hint 显示未见过
      None  — hint 未启用或 Redis 异常（调用方忽略预检，直接查 CH）
    """
    try:
        result = await redis.sismember(KEY, trace_id)
        return bool(result)
    except Exception:
        logger.debug("trace_hint.maybe_seen: Redis error, hint unavailable")
        return None
