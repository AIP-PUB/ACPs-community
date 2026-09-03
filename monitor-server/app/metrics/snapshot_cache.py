"""app/metrics/snapshot_cache.py — Redis 最新快照读写访问层（设计 §4.3）。

Hash payload + ZSet 索引；写入侧（Writer 刷新）与读取侧（snapshots/query）共用。
写入原子性约束：Hash 与 ZSet 必须同一原子单元（§4.3）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

import structlog
from acps_sdk.amp.models import LoadMetrics, WindowMetrics
from redis.asyncio import Redis

from app.metrics.cursor import SnapshotCursor

logger = structlog.get_logger(__name__)

# ── 键定义 ─────────────────────────────────────────────────────────────────────

SNAPSHOT_INDEX_KEY: Final = "amp:metrics:snapshot:index"
"""ZSet 索引键；score = observed_at_ms；member = aic。"""


def snapshot_hash_key(aic: str) -> str:
    """返回单个 Agent 快照 Hash 的键名。"""
    return f"amp:metrics:snapshot:{aic}"


# ── 数据类 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CachedSnapshot:
    """Redis 缓存的最新快照（§4.3）。"""

    aic: str
    observed_at_ms: int
    uptime_seconds: float | None
    load_metrics: LoadMetrics | None
    window_metrics: list[WindowMetrics] | None
    service_name: str | None
    service_namespace: str | None
    deployment_env: str | None


# ── 写入（Writer 专用） ────────────────────────────────────────────────────────

_LUA_UPSERT = """
local hash_key = KEYS[1]
local index_key = KEYS[2]
local aic = ARGV[1]
local new_score = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local observed_at    = ARGV[4]
local uptime         = ARGV[5]
local load_json      = ARGV[6]
local window_json    = ARGV[7]
local service_name   = ARGV[8]
local service_ns     = ARGV[9]
local deploy_env     = ARGV[10]

-- 仅当 new_score >= 现有 ZSet score 时才覆盖（防迟到事件回退最新快照）
local existing_score = redis.call('ZSCORE', index_key, aic)
if existing_score and tonumber(existing_score) > new_score then
    return 0
end

redis.call('HSET', hash_key,
    'observed_at', observed_at,
    'uptime_seconds', uptime,
    'load_metrics_json', load_json,
    'window_metrics_json', window_json,
    'service_name', service_name,
    'service_namespace', service_ns,
    'deployment_env', deploy_env
)
redis.call('EXPIRE', hash_key, ttl)
redis.call('ZADD', index_key, new_score, aic)
return 1
"""


async def upsert_snapshot(redis: Redis, snap: CachedSnapshot) -> None:
    """Writer 专用：Remote Write 成功后原子刷新快照（§4.3、C-METRIC-WRITE-1）。

    使用 Lua 脚本保证 Hash 写入 + ZSet 更新同一原子单元，
    且仅当 new_score >= 已存 score 时才覆盖（防迟到事件覆盖最新快照）。

    Args:
        redis: Redis 客户端。
        snap: 待写入的快照。
    """
    from app.core.config import get_settings

    ttl = get_settings().metrics_snapshot_ttl_seconds
    hash_key = snapshot_hash_key(snap.aic)

    load_json = snap.load_metrics.model_dump_json(by_alias=True) if snap.load_metrics else ""
    window_json = (
        json.dumps([wm.model_dump(by_alias=True) for wm in snap.window_metrics]) if snap.window_metrics else ""
    )

    await redis.eval(
        _LUA_UPSERT,
        2,
        hash_key,
        SNAPSHOT_INDEX_KEY,
        snap.aic,
        str(snap.observed_at_ms),
        str(ttl),
        str(snap.observed_at_ms),
        str(snap.uptime_seconds) if snap.uptime_seconds is not None else "",
        load_json,
        window_json,
        snap.service_name or "",
        snap.service_namespace or "",
        snap.deployment_env or "",
    )


def _parse_snapshot(aic: str, raw: dict[Any, Any]) -> CachedSnapshot | None:
    """从 HGETALL 结果解析 CachedSnapshot（JSON 失败返回 None）。"""
    if not raw:
        return None
    try:
        observed_at_ms = int(raw.get("observed_at") or "0") or None
        if observed_at_ms is None:
            return None

        uptime_seconds: float | None = None
        if raw.get("uptime_seconds"):
            uptime_seconds = float(raw["uptime_seconds"])

        load_metrics: LoadMetrics | None = None
        if raw.get("load_metrics_json"):
            load_metrics = LoadMetrics.model_validate_json(raw["load_metrics_json"])

        window_metrics: list[WindowMetrics] | None = None
        if raw.get("window_metrics_json"):
            wm_data = json.loads(raw["window_metrics_json"])
            window_metrics = [WindowMetrics.model_validate(w) for w in wm_data]

        return CachedSnapshot(
            aic=aic,
            observed_at_ms=observed_at_ms,
            uptime_seconds=uptime_seconds,
            load_metrics=load_metrics,
            window_metrics=window_metrics,
            service_name=raw.get("service_name") or None,
            service_namespace=raw.get("service_namespace") or None,
            deployment_env=raw.get("deployment_env") or None,
        )
    except Exception:
        logger.warning("snapshot_cache.parse_failed", aic=aic, exc_info=True)
        return None


# ── 读取 ──────────────────────────────────────────────────────────────────────


async def get_snapshot(redis: Redis, aic: str) -> CachedSnapshot | None:
    """读取单个 Agent 的最新快照（HGETALL）。

    Args:
        redis: Redis 客户端。
        aic: Agent Identity Code。

    Returns:
        CachedSnapshot | None（键不存在或 JSON 解析失败返回 None）。
    """
    raw: dict[Any, Any] = await redis.hgetall(snapshot_hash_key(aic))
    return _parse_snapshot(aic, raw)


async def mget_snapshots(redis: Redis, aics: list[str]) -> list[CachedSnapshot | None]:
    """批量读取多个 Agent 的最新快照（pipeline HGETALL）。

    Args:
        redis: Redis 客户端。
        aics: AIC 列表（保持顺序）。

    Returns:
        list[CachedSnapshot | None]: 与入参同顺序，None 表示不存在或解析失败。
    """
    if not aics:
        return []
    async with redis.pipeline(transaction=False) as pipe:
        for aic in aics:
            await pipe.hgetall(snapshot_hash_key(aic))
        results = await pipe.execute()
    return [_parse_snapshot(aic, raw) for aic, raw in zip(aics, results, strict=True)]


async def scan_index_desc(
    redis: Redis,
    *,
    cursor: SnapshotCursor | None,
    batch_size: int,
) -> list[tuple[str, int]]:
    """按 observedAt desc、aic asc 稳定顺序从 ZSet 索引中取一批条目（§4.3）。

    Args:
        redis: Redis 客户端。
        cursor: 上一页末项（None 表示从头开始）。
        batch_size: 单批拉取条数。

    Returns:
        list[tuple[str, int]]: [(aic, observed_at_ms)] 降序列表。
    """
    # 含 cursor 分数上界（同 score 组内由 cursor.aic 过滤，§6.12 / 设计 §6.11）
    score_max = str(cursor.observed_at_ms) if cursor else "+inf"
    # 多取一些再在内存排序/截断：Redis 同 score 为逆字典序，与设计 (observedAt desc, aic asc) 不一致
    fetch_size = min(max(batch_size * 4, batch_size + 8), 5000)
    raw = await redis.zrevrangebyscore(
        SNAPSHOT_INDEX_KEY,
        max=score_max,
        min="-inf",
        withscores=True,
        start=0,
        num=fetch_size,
    )
    raw_items: list[tuple[Any, float]] = raw  # type: ignore[assignment]
    items = [(str(member), int(score)) for member, score in raw_items]
    items.sort(key=lambda item: (-item[1], item[0]))
    if cursor is not None:
        items = [
            (aic, score)
            for aic, score in items
            if score < cursor.observed_at_ms or (score == cursor.observed_at_ms and aic > cursor.aic)
        ]
    return items[:batch_size]


async def remove_index_entry(redis: Redis, aic: str) -> None:
    """清理悬挂的 ZSet 索引项（Hash 缺失但索引仍在）。

    §4.3 约束：Provider 不得把"只有索引没有 payload"的条目返回给调用方。
    """
    await redis.zrem(SNAPSHOT_INDEX_KEY, aic)


async def backfill_snapshot(redis: Redis, snap: CachedSnapshot) -> None:
    """TSDB 修复成功后异步回写（snapshots/query 第 7 步）。

    失败不抛出，只 WARNING（§2.2：回写不阻塞主查询路径）。
    """
    try:
        await upsert_snapshot(redis, snap)
    except Exception:
        logger.warning("snapshot_cache.backfill_failed", aic=snap.aic, exc_info=True)


__all__ = [
    "SNAPSHOT_INDEX_KEY",
    "CachedSnapshot",
    "backfill_snapshot",
    "get_snapshot",
    "mget_snapshots",
    "remove_index_entry",
    "scan_index_desc",
    "snapshot_hash_key",
    "upsert_snapshot",
]
