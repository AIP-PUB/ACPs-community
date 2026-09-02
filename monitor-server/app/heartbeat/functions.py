"""Heartbeat 模块 — Redis Functions 加载器与类型化调用包装（C-WRITE-1）。

业务代码只能通过本模块触达六个 Function；禁止直接 redis.execute_command 调用
Lua 状态机（code review 约定）。
"""

from __future__ import annotations

import importlib.resources
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal

import structlog
from redis.asyncio import Redis

from app.core.config import settings
from app.heartbeat.redis_keys import (
    delta_outbox_key,
    delta_seq_key,
    latest_key,
    liveness_zset_key,
    published_seq_key,
    relay_epoch_key,
)
from app.heartbeat.sharding import shard_id_for_aic

logger = structlog.get_logger(__name__)

FUNCTIONS_LIB_NAME: Final = "amp_heartbeat"


@dataclass(frozen=True)
class ApplyResult:
    """hb_apply_heartbeat 的返回值。"""

    status: Literal["ignored_older", "applied", "applied_with_delta"]
    kind: Literal["enter_alive", "refresh_alive"] | None
    seq: int | None


@dataclass(frozen=True)
class ReconcileResult:
    """hb_mark_silent_one / hb_evict_one 的返回值。"""

    status: str
    seq: int | None


async def ensure_functions_loaded(redis: Redis) -> None:
    """加载（或重新加载）Heartbeat Redis Functions 库（幂等，FUNCTION LOAD REPLACE）。

    从包内 lua/hb_functions.lua 读取脚本，适配打包后路径。
    启动时由 HeartbeatRuntime.start() 调用。

    Args:
        redis: 已初始化的 Redis 异步客户端。
    """
    lua_src = importlib.resources.files("app.heartbeat.lua").joinpath("hb_functions.lua").read_text("utf-8")
    await redis.execute_command("FUNCTION", "LOAD", "REPLACE", lua_src)  # type: ignore[no-untyped-call]
    logger.info("Redis Functions 已加载", lib=FUNCTIONS_LIB_NAME)


async def apply_heartbeat(
    redis: Redis,
    *,
    aic: str,
    observed_at_ms: int,
    source_timestamp_ms: int | None = None,
) -> ApplyResult:
    """调用 hb_apply_heartbeat，写入心跳当前态（C-WRITE-1）。

    observed_at_iso 由本函数从 observed_at_ms 格式化（UTC ISO 8601），
    阈值从 settings 读取。

    Args:
        redis: Redis 客户端。
        aic: Agent Identity Code。
        observed_at_ms: 心跳时间戳（epoch ms）。
        source_timestamp_ms: 可选来源时间戳（仅诊断用）。

    Returns:
        ApplyResult dataclass 封装的写入结果。
    """
    shard = shard_id_for_aic(aic)
    observed_at_iso = datetime.fromtimestamp(observed_at_ms / 1000, tz=UTC).isoformat()
    source_ts_str = str(source_timestamp_ms) if source_timestamp_ms is not None else ""

    keys = [
        latest_key(shard, aic),
        liveness_zset_key(shard),
        delta_seq_key(shard),
        delta_outbox_key(shard),
    ]
    argv = [
        aic,
        str(observed_at_ms),
        observed_at_iso,
        source_ts_str,
        str(settings.heartbeat_refresh_emit_interval_seconds * 1000),
        str(settings.heartbeat_outbox_max_len),
    ]

    raw = await redis.fcall("hb_apply_heartbeat", len(keys), *keys, *argv)
    status, kind_raw, seq_raw = raw[0], raw[1], raw[2]

    kind: Literal["enter_alive", "refresh_alive"] | None = None
    if kind_raw in ("enter_alive", "refresh_alive"):
        kind = kind_raw

    seq = int(seq_raw) if seq_raw and int(seq_raw) > 0 else None

    return ApplyResult(status=status, kind=kind, seq=seq)


async def mark_silent_one(
    redis: Redis,
    *,
    shard: str,
    aic: str,
) -> ReconcileResult:
    """调用 hb_mark_silent_one，将超阈值 AIC 转为 left_alive（C-WRITE-1）。

    Args:
        redis: Redis 客户端。
        shard: 目标 shard id。
        aic: Agent Identity Code。

    Returns:
        ReconcileResult，status ∈ {skipped_missing, skipped_membership, skipped_refreshed, left_alive}。
    """
    keys = [
        latest_key(shard, aic),
        liveness_zset_key(shard),
        delta_seq_key(shard),
        delta_outbox_key(shard),
    ]
    argv = [
        aic,
        str(settings.heartbeat_silence_threshold_seconds * 1000),
        str(settings.heartbeat_outbox_max_len),
    ]
    raw = await redis.fcall("hb_mark_silent_one", len(keys), *keys, *argv)
    status, seq_raw = raw[0], raw[1]
    seq = int(seq_raw) if seq_raw and int(seq_raw) > 0 else None
    return ReconcileResult(status=status, seq=seq)


async def evict_one(
    redis: Redis,
    *,
    shard: str,
    aic: str,
) -> ReconcileResult:
    """调用 hb_evict_one，删除超过 evict 阈值的 AIC 记录（C-WRITE-1）。

    Args:
        redis: Redis 客户端。
        shard: 目标 shard id。
        aic: Agent Identity Code。

    Returns:
        ReconcileResult，status ∈ {skipped_missing, skipped_refreshed, evicted, evicted_with_repair}。
    """
    keys = [
        latest_key(shard, aic),
        liveness_zset_key(shard),
        delta_seq_key(shard),
        delta_outbox_key(shard),
    ]
    argv = [
        aic,
        str(settings.heartbeat_evict_after_seconds * 1000),
        str(settings.heartbeat_outbox_max_len),
    ]
    raw = await redis.fcall("hb_evict_one", len(keys), *keys, *argv)
    status, seq_raw = raw[0], raw[1]
    seq = int(seq_raw) if seq_raw and int(seq_raw) > 0 else None
    return ReconcileResult(status=status, seq=seq)


async def relay_commit_published_seq(
    redis: Redis,
    *,
    shard: str,
    epoch: int,
    seq: int,
) -> bool:
    """调用 hb_relay_commit，写入 published_seq（epoch 守卫，C-RELAY-1）。

    Args:
        redis: Redis 客户端。
        shard: 目标 shard id。
        epoch: 当前 relay 届次（由 INCR relay_epoch_key 得到）。
        seq: 已确认发布的最大 delta seq。

    Returns:
        True 表示写入成功；False 表示 epoch 过期（被 fencing）。
    """
    keys = [published_seq_key(shard), relay_epoch_key(shard)]
    argv = [str(epoch), str(seq)]
    result = await redis.fcall("hb_relay_commit", len(keys), *keys, *argv)
    return int(result) == 1


async def relay_ack(
    redis: Redis,
    *,
    shard: str,
    epoch: int,
    entry_id: str,
) -> bool:
    """调用 hb_relay_ack，执行 XACK（epoch 守卫，C-RELAY-1）。

    relay._publish_batch 每条成功发布后调用，防止旧 owner 的 XACK 消除
    新 owner PEL 中待 republish 的条目。

    Args:
        redis: Redis 客户端。
        shard: 目标 shard id。
        epoch: 当前 relay 届次。
        entry_id: Redis Stream entry id。

    Returns:
        True 表示 XACK 成功；False 表示 epoch 过期。
    """
    from app.heartbeat.relay import OUTBOX_CONSUMER_GROUP

    keys = [delta_outbox_key(shard), relay_epoch_key(shard)]
    argv = [str(epoch), entry_id, OUTBOX_CONSUMER_GROUP]
    result = await redis.fcall("hb_relay_ack", len(keys), *keys, *argv)
    return int(result) == 1


async def relay_trim(
    redis: Redis,
    *,
    shard: str,
    epoch: int,
    min_entry_id: str,
) -> bool:
    """调用 hb_relay_trim，裁剪 outbox stream（epoch 守卫，C-RELAY-1）。

    防止僵尸 relay 裁掉新 owner 尚未 republish 的 PEL 条目。

    Args:
        redis: Redis 客户端。
        shard: 目标 shard id。
        epoch: 当前 relay 届次。
        min_entry_id: XTRIM MINID 参数（已 XACK 的最新 entry id）。

    Returns:
        True 表示 XTRIM 成功；False 表示 epoch 过期。
    """
    keys = [delta_outbox_key(shard), relay_epoch_key(shard)]
    argv = [str(epoch), min_entry_id]
    result = await redis.fcall("hb_relay_trim", len(keys), *keys, *argv)
    return int(result) == 1
