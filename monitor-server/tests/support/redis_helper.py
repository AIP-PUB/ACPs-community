"""tests/support/redis_helper.py — Heartbeat & Metrics 集成测试 Redis 辅助函数。"""

from __future__ import annotations

from redis.asyncio import Redis

from app.heartbeat.functions import ApplyResult, apply_heartbeat, ensure_functions_loaded
from app.heartbeat.redis_keys import delta_outbox_key, published_seq_key


async def reset_heartbeat_redis_state(redis: Redis) -> None:
    """删除所有 amp:hb:* 键，但保留已加载的 Redis Functions（避免重新加载开销）。

    Args:
        redis: 已初始化的 Redis 异步客户端。
    """
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match="amp:hb:*", count=200)
        if keys:
            await redis.delete(*keys)
        if cursor == 0:
            break


async def seed_heartbeat(
    redis: Redis,
    *,
    aic: str,
    observed_at_ms: int,
    source_timestamp_ms: int | None = None,
) -> ApplyResult:
    """向 Redis 注入一条心跳记录（需要 Redis Functions 已加载）。

    Args:
        redis: Redis 客户端。
        aic: Agent Identity Code。
        observed_at_ms: 心跳时间戳（epoch ms）。
        source_timestamp_ms: 可选来源时间戳。

    Returns:
        apply_heartbeat 的返回值。
    """
    return await apply_heartbeat(
        redis,
        aic=aic,
        observed_at_ms=observed_at_ms,
        source_timestamp_ms=source_timestamp_ms,
    )


async def read_outbox(redis: Redis, shard: str) -> list[dict[str, str]]:
    """读取指定 shard 的 delta outbox 中所有条目（stream XRANGE 全量）。

    Args:
        redis: Redis 客户端。
        shard: 分片 id，如 "hb-000"。

    Returns:
        条目列表，每项为 {field: value} dict。
    """
    key = delta_outbox_key(shard)
    entries = await redis.xrange(key, "-", "+")
    return [fields for _, fields in entries]  # type: ignore[misc, union-attr]


async def read_published_seq(redis: Redis, shard: str) -> int:
    """读取指定 shard 的 published_seq（缺失返回 0）。

    Args:
        redis: Redis 客户端。
        shard: 分片 id。

    Returns:
        published_seq 整数值，缺失时为 0。
    """
    raw = await redis.get(published_seq_key(shard))
    return int(raw) if raw is not None else 0


async def ensure_functions_for_tests(redis: Redis) -> None:
    """确保 Heartbeat Redis Functions 已加载（集成测试 fixture 使用）。

    幂等：FUNCTION LOAD REPLACE 不重复加载。

    Args:
        redis: Redis 客户端。
    """
    await ensure_functions_loaded(redis)


# ── Metrics 辅助 ──────────────────────────────────────────────────────────────


async def reset_metrics_redis_state(redis: Redis) -> None:
    """删除所有 amp:metrics:* 键（集成测试前清理环境）。

    Args:
        redis: 已初始化的 Redis 异步客户端。
    """
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match="amp:metrics:*", count=200)
        if keys:
            await redis.delete(*keys)
        if cursor == 0:
            break


# ── Access 辅助 ────────────────────────────────────────────────────────────────


async def reset_access_redis_state(redis: Redis) -> None:
    """删除所有 amp:access:* 键（Access 集成测试前清理环境）。

    Args:
        redis: 已初始化的 Redis 异步客户端。
    """
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match="amp:access:*", count=200)
        if keys:
            await redis.delete(*keys)
        if cursor == 0:
            break


async def seed_snapshot(
    redis: Redis,
    *,
    aic: str,
    observed_at_ms: int,
    service_name: str | None = None,
    service_namespace: str | None = None,
    deployment_env: str | None = None,
) -> None:
    """向 Redis 注入一条快照缓存条目（集成测试 fixture 使用）。

    Args:
        redis: Redis 客户端。
        aic: Agent Identity Code。
        observed_at_ms: 快照时间戳（epoch ms）。
        service_name: 可选服务名。
        service_namespace: 可选命名空间。
        deployment_env: 可选部署环境。
    """
    from app.metrics.snapshot_cache import CachedSnapshot, upsert_snapshot

    snap = CachedSnapshot(
        aic=aic,
        observed_at_ms=observed_at_ms,
        uptime_seconds=None,
        load_metrics=None,
        window_metrics=None,
        service_name=service_name,
        service_namespace=service_namespace,
        deployment_env=deployment_env,
    )
    await upsert_snapshot(redis, snap)


async def reset_message_redis_state(redis: Redis) -> None:
    """删除所有 amp:message:* 键（Message 集成/E2E 测试前清理环境）。"""
    for pattern in ("amp:message:wm:*", "amp:message:dedupe:*"):
        cursor = 0
        while True:
            cursor, keys = await redis.scan(cursor, match=pattern, count=200)
            if keys:
                await redis.delete(*keys)
            if cursor == 0:
                break


async def seed_watermark(redis: Redis, observed_at_ms: int) -> None:
    """直接写入 dataFreshnessAt 水位（集成测试用，绕过 advance_watermark Lua 脚本）。

    Args:
        redis: 已初始化的 Redis 异步客户端。
        observed_at_ms: 要设置的水位时间戳（毫秒）。
    """
    from app.metrics.freshness import DATA_FRESHNESS_KEY

    await redis.set(DATA_FRESHNESS_KEY, str(observed_at_ms))


# ── System 辅助 ────────────────────────────────────────────────────────────────


async def reset_system_redis_state(redis: Redis) -> None:
    """清理 amp:system:wm:* 键（System E2E/集成测试前清理水位）。

    Args:
        redis: 已初始化的 Redis 异步客户端。
    """
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match="amp:system:wm:*", count=200)
        if keys:
            await redis.delete(*keys)
        if cursor == 0:
            break
