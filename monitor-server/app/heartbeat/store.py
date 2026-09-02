"""Heartbeat 模块 — Redis 只读访问层（C-QUERY-4）。

查询路径只读不写；统一返回类型化 dataclass，屏蔽字段名细节。
所有时间均通过 redis_now_ms() 取 Redis TIME（C-TIME-3）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import structlog
from redis.asyncio import Redis

from app.heartbeat.redis_keys import (
    WATERMARKS_KEY,
    delta_outbox_key,
    latest_key,
    liveness_zset_key,
    published_seq_key,
)
from app.heartbeat.sharding import all_shard_ids

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class LatestEntry:
    """hb_apply_heartbeat 写入的心跳当前态记录（HGETALL 反序列化）。"""

    aic: str
    last_seen_at_ms: int
    last_seen_at: str
    source_timestamp_ms: int | None
    last_delta_seen_at_ms: int | None
    last_delta_seq: int | None
    alive_membership_state: Literal["alive", "left_alive"]


@dataclass(frozen=True)
class SnapshotRow:
    """mget_snapshot_fields 专用轻量行（§7.3 步骤 2，不含 last_seen_at_ms）。"""

    aic: str
    last_delta_seq: int | None
    last_seen_at: str | None
    source_timestamp_ms: int | None
    alive_membership_state: Literal["alive", "left_alive"]


def _parse_latest_entry(aic: str, data: dict[str, str]) -> LatestEntry:
    """从 HGETALL dict 解析 LatestEntry。"""
    return LatestEntry(
        aic=aic,
        last_seen_at_ms=int(data["last_seen_at_ms"]),
        last_seen_at=data.get("last_seen_at", ""),
        source_timestamp_ms=int(data["source_timestamp_ms"]) if "source_timestamp_ms" in data else None,
        last_delta_seen_at_ms=int(data["last_delta_seen_at_ms"]) if "last_delta_seen_at_ms" in data else None,
        last_delta_seq=int(data["last_delta_seq"]) if "last_delta_seq" in data else None,
        alive_membership_state=data.get("alive_membership_state", "alive"),  # type: ignore[arg-type]
    )


async def redis_now_ms(redis: Redis) -> int:
    """通过 Redis TIME 命令获取当前时间（epoch ms，C-TIME-3）。

    所有 evaluatedAt / snapshotNowMs 的唯一来源。

    Args:
        redis: Redis 客户端。

    Returns:
        当前 Redis 时间的 epoch 毫秒数。
    """
    t = await redis.time()
    return int(t[0]) * 1000 + int(t[1]) // 1000


async def get_latest(redis: Redis, shard: str, aic: str) -> LatestEntry | None:
    """通过 HGETALL 获取单个 AIC 的心跳当前态。

    Args:
        redis: Redis 客户端。
        shard: AIC 所属分片 id。
        aic: Agent Identity Code。

    Returns:
        LatestEntry 或 None（不存在时）。
    """
    data: dict[str, str] = await redis.hgetall(latest_key(shard, aic))  # type: ignore[assignment]
    if not data:
        return None
    return _parse_latest_entry(aic, data)


async def mget_latest(redis: Redis, shard: str, aics: list[str]) -> list[LatestEntry | None]:
    """批量获取同一 shard 内多个 AIC 的心跳当前态（pipeline HGETALL，保持顺序）。

    Args:
        redis: Redis 客户端。
        shard: AIC 所属分片 id。
        aics: AIC 列表。

    Returns:
        与入参顺序对应的 LatestEntry | None 列表。
    """
    if not aics:
        return []
    pipe = redis.pipeline(transaction=False)
    for aic in aics:
        pipe.hgetall(latest_key(shard, aic))
    results: list[dict[str, str]] = await pipe.execute()
    return [_parse_latest_entry(aic, data) if data else None for aic, data in zip(aics, results, strict=True)]


async def mget_snapshot_fields(redis: Redis, shard: str, aics: list[str]) -> list[SnapshotRow | None]:
    """批量获取 snapshot 所需字段（§7.3 步骤 2 专用，不含全量字段）。

    Args:
        redis: Redis 客户端。
        shard: 分片 id。
        aics: AIC 列表。

    Returns:
        与入参顺序对应的 SnapshotRow | None 列表。
    """
    if not aics:
        return []
    pipe = redis.pipeline(transaction=False)
    for aic in aics:
        pipe.hmget(
            latest_key(shard, aic),
            "last_delta_seq",
            "last_seen_at",
            "source_timestamp_ms",
            "alive_membership_state",
        )
    results: list[list[str | None]] = await pipe.execute()
    rows: list[SnapshotRow | None] = []
    for aic, fields in zip(aics, results, strict=True):
        if all(f is None for f in fields):
            rows.append(None)
        else:
            rows.append(
                SnapshotRow(
                    aic=aic,
                    last_delta_seq=int(fields[0]) if fields[0] is not None else None,
                    last_seen_at=fields[1],
                    source_timestamp_ms=int(fields[2]) if fields[2] is not None else None,
                    alive_membership_state=fields[3] or "alive",  # type: ignore[arg-type]
                )
            )
    return rows


async def zcard(redis: Redis, shard: str) -> int:
    """获取 liveness_zset 的元素总数（§10.1 指标采样）。

    Args:
        redis: Redis 客户端。
        shard: 分片 id。

    Returns:
        zset 中的元素数量。
    """
    return await redis.zcard(liveness_zset_key(shard))


async def zcount_score_at_least(redis: Redis, shard: str, min_score_ms: int) -> int:
    """统计 liveness_zset 中 score >= min_score_ms 的元素数（含界）。

    用于 alive 计数（silence_ms <= silence_threshold_ms）和 §10.1 指标采样。

    Args:
        redis: Redis 客户端。
        shard: 分片 id。
        min_score_ms: 下界 epoch ms（含界）。

    Returns:
        满足条件的元素数。
    """
    return await redis.zcount(liveness_zset_key(shard), min_score_ms, "+inf")


async def zrange_by_score(
    redis: Redis,
    shard: str,
    *,
    lower: str,
    upper: str,
    limit: int,
    with_scores: bool = True,
) -> list[tuple[str, int]]:
    """升序扫描 liveness_zset（ZRANGEBYSCORE）。

    lower/upper 直接传 Redis 语法字符串（"-inf"、"(123"、"456"），
    由调用方决定含界/排他。用于 reconciler 候选扫描、silence_top 聚合、snapshot 枚举。

    Args:
        redis: Redis 客户端。
        shard: 分片 id。
        lower: 分数下界（Redis ZRANGEBYSCORE 语法）。
        upper: 分数上界（Redis ZRANGEBYSCORE 语法）。
        limit: 返回上限。
        with_scores: 是否返回分数。

    Returns:
        (aic, score_ms) 元组列表，按分数升序。
    """
    if with_scores:
        raw = await redis.zrangebyscore(
            liveness_zset_key(shard),
            lower,
            upper,
            withscores=True,
            start=0,
            num=limit,
        )
        return [(str(member), int(score)) for member, score in raw]  # type: ignore[str-unpack]
    raw = await redis.zrangebyscore(
        liveness_zset_key(shard),
        lower,
        upper,
        start=0,
        num=limit,
    )
    return [(str(member), 0) for member in raw]


async def zrevrange_by_score(
    redis: Redis,
    shard: str,
    *,
    lower: str,
    upper: str,
    limit: int,
    with_scores: bool = True,
) -> list[tuple[str, int]]:
    """降序扫描 liveness_zset（ZREVRANGEBYSCORE，修复审查 P2-6）。

    参数含义同 zrange_by_score，但 upper 优先（score 大→小）。
    用于 /liveness/query 按 lastSeenAt desc 排序的 cursor 路径。

    Args:
        redis: Redis 客户端。
        shard: 分片 id。
        lower: 分数下界（Redis ZREVRANGEBYSCORE 语法，lower=min）。
        upper: 分数上界（Redis ZREVRANGEBYSCORE 语法，upper=max，高优先）。
        limit: 返回上限。
        with_scores: 是否返回分数。

    Returns:
        (aic, score_ms) 元组列表，按分数降序。
    """
    if with_scores:
        raw = await redis.zrevrangebyscore(
            liveness_zset_key(shard),
            upper,
            lower,
            withscores=True,
            start=0,
            num=limit,
        )
        return [(str(member), int(score)) for member, score in raw]  # type: ignore[str-unpack]
    raw = await redis.zrevrangebyscore(
        liveness_zset_key(shard),
        upper,
        lower,
        start=0,
        num=limit,
    )
    return [(str(member), 0) for member in raw]


async def zrange_score_group(
    redis: Redis,
    shard: str,
    score: int,
) -> list[tuple[str, int]]:
    """读取 liveness_zset 中 score 精确等于 score 的全部条目（snapshot tie-safe，C-SYNC-2）。

    ZRANGEBYSCORE key S S（不加 LIMIT）。

    Args:
        redis: Redis 客户端。
        shard: 分片 id。
        score: 精确分数（epoch ms）。

    Returns:
        (aic, score) 元组列表。
    """
    raw = await redis.zrangebyscore(liveness_zset_key(shard), score, score, withscores=True)
    return [(str(member), int(sc)) for member, sc in raw]  # type: ignore[str-unpack]


async def read_watermarks(redis: Redis) -> dict[int, tuple[int, int]]:
    """读取 writer 水位哈希表（HGETALL amp:hb:writer_watermarks）。

    value 编码 "<watermark_ms>:<updated_at_ms>"（§4.2）。

    Args:
        redis: Redis 客户端。

    Returns:
        {partition: (watermark_ms, updated_at_ms)} dict，空时返回 {}。
    """
    raw: dict[str, str] = await redis.hgetall(WATERMARKS_KEY)  # type: ignore[assignment]
    result: dict[int, tuple[int, int]] = {}
    for field, value in raw.items():
        try:
            partition = int(field)
            parts = value.split(":")
            if len(parts) == 2:
                result[partition] = (int(parts[0]), int(parts[1]))
        except ValueError, IndexError:
            continue
    return result


async def write_watermarks(redis: Redis, entries: dict[int, tuple[int, int]]) -> None:
    """写入 writer 水位（HSET 多字段，writer 专用）。

    Args:
        redis: Redis 客户端。
        entries: {partition: (watermark_ms, updated_at_ms)} dict。
    """
    if not entries:
        return
    fields: dict[str, str] = {}
    for partition, (watermark_ms, updated_at_ms) in entries.items():
        fields[str(partition)] = f"{watermark_ms}:{updated_at_ms}"
    await redis.hset(WATERMARKS_KEY, mapping=fields)  # type: ignore[arg-type]


async def read_published_seq(redis: Redis, shard: str) -> int:
    """读取 published_seq（缺失视为 0，§4.3）。

    Args:
        redis: Redis 客户端。
        shard: 分片 id。

    Returns:
        published_seq 值，缺失时为 0。
    """
    raw = await redis.get(published_seq_key(shard))
    return int(raw) if raw is not None else 0


async def read_all_published_seq(redis: Redis) -> dict[str, int]:
    """读取所有分片的 published_seq（pipeline，全 shard）。

    Args:
        redis: Redis 客户端。

    Returns:
        {shard_id: published_seq} dict，缺失分片值为 0。
    """
    shards = all_shard_ids()
    if not shards:
        return {}
    pipe = redis.pipeline(transaction=False)
    for shard in shards:
        pipe.get(published_seq_key(shard))
    results: list[str | None] = await pipe.execute()
    return {shard: int(raw) if raw is not None else 0 for shard, raw in zip(shards, results, strict=True)}


async def outbox_publish_lag_ms(redis: Redis, shard: str) -> int | None:
    """计算 outbox 的最大发布延迟（ms）。

    覆盖两种积压来源（修复审查 P1-2，旧 PEL-only 方案盲区）：
    ① PEL 积压：relay 崩溃时 XACK 前已投递但未确认的条目。
    ② 未投递积压：relay 彻底停摆，新产出条目从未被 XREADGROUP 取走。

    最终取两者中的较大值（更旧的那个）作为真实 publish lag 上界。

    Args:
        redis: Redis 客户端。
        shard: 分片 id。

    Returns:
        发布延迟 ms（>=0），outbox 为空时返回 None。
    """
    outbox_key = delta_outbox_key(shard)
    group_name = "amp-hb-relay"
    now_ms = await redis_now_ms(redis)

    lag_from_pel: int | None = None
    lag_from_undelivered: int | None = None
    group_found: bool = False

    # ① PEL 积压：XPENDING outbox group - + 1
    try:
        pending = await redis.xpending_range(outbox_key, group_name, min="-", max="+", count=1)
        if pending:
            oldest_id: str = str(pending[0]["message_id"])
            ms_part = int(oldest_id.split("-")[0])
            lag_from_pel = now_ms - ms_part
    except Exception:
        logger.debug("outbox_publish_lag_ms: PEL probe failed", shard=shard)

    # ② 未投递积压：通过 XINFO GROUPS 找 last-delivered-id，再 XRANGE 取下一条
    try:
        groups_info = await redis.xinfo_groups(outbox_key)
        for group_info in groups_info:
            gname = group_info.get("name", "")
            if gname != group_name:
                continue
            group_found = True
            last_delivered = group_info.get("last-delivered-id", "0-0")
            # 从 last_delivered 之后读 1 条
            next_entries = await redis.xrange(outbox_key, f"({last_delivered}", "+", count=1)
            if next_entries:
                next_id = str(next_entries[0][0])
                ms_part = int(next_id.split("-")[0])
                lag_from_undelivered = now_ms - ms_part
            break
    except Exception:
        logger.debug("outbox_publish_lag_ms: XINFO GROUPS probe failed", shard=shard)

    # 取两者较大值（更旧/更积压的那个）
    if lag_from_pel is None and lag_from_undelivered is None:
        if group_found:
            # consumer group 存在且已全部投递+确认，lag 为 0
            return None
        # outbox 没有 consumer group——检查 outbox 是否有未被任何 group 取走的条目
        try:
            entries = await redis.xrange(outbox_key, "-", "+", count=1)
            if entries:
                oldest_id_str: str = str(entries[0][0])
                ms_part = int(oldest_id_str.split("-")[0])
                return max(0, now_ms - ms_part)
        except Exception:
            logger.debug("outbox_publish_lag_ms: XRANGE fallback failed", shard=shard)
        return None

    candidates = [v for v in [lag_from_pel, lag_from_undelivered] if v is not None]
    return max(candidates) if candidates else None
