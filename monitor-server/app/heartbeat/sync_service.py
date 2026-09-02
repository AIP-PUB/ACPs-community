"""Heartbeat Sync API 业务逻辑层（§7.1.2 Sync 部分，§8）。

职责：
1. ensure_sync_enabled() — sync_enabled 开关守卫（SyncDisabledError 404）
2. get_sync_info(redis) — 返回 HeartbeatSyncInfo（含 currentPublishedSeqByShard）
3. stream_snapshot(redis, exporter) — delta log 健康检查后，委托 exporter.stream()
4. _ensure_delta_log_healthy(redis) — 基于 outbox_publish_lag_ms 检查（P1-2，8-7）

P1-4：所有 Redis 异常在本层捕获 → SnapshotUnavailableError。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import structlog
from acps_sdk.amp.heartbeat_sync import (
    ALIVE_DELTA_SCHEMA_VERSION,
    ALIVE_DELTA_TYPE,
    SNAPSHOT_CONTENT_TYPE,
    HeartbeatSyncInfo,
    seq_to_str,
)
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.core.config import settings
from app.heartbeat import store
from app.heartbeat.exception import (
    DeltaLogUnhealthyError,
    SnapshotUnavailableError,
    SyncDisabledError,
)
from app.heartbeat.sharding import all_shard_ids
from app.heartbeat.snapshot import SnapshotExporter

logger = structlog.get_logger(__name__)


def ensure_sync_enabled() -> None:
    """确认 Sync Profile 已启用，否则抛出 SyncDisabledError（404）。

    Raises:
        SyncDisabledError: sync_enabled=False 时。
    """
    if not settings.heartbeat_sync_enabled:
        raise SyncDisabledError()


async def get_sync_info(redis: Redis) -> HeartbeatSyncInfo:
    """返回 alive-delta Sync Profile 元信息（GET /sync/info）。

    包含 currentPublishedSeqByShard，消费者可据此定位 Kafka 偏移。

    Args:
        redis: Redis 客户端。

    Returns:
        HeartbeatSyncInfo Pydantic 模型。

    Raises:
        SnapshotUnavailableError: Redis 连接异常（P1-4）。
    """
    try:
        published_seq = await store.read_all_published_seq(redis)
    except (RedisConnectionError, RedisTimeoutError) as exc:
        raise SnapshotUnavailableError(str(exc)) from exc

    return HeartbeatSyncInfo(
        type=ALIVE_DELTA_TYPE,
        schema_version=ALIVE_DELTA_SCHEMA_VERSION,
        snapshot_content_type=SNAPSHOT_CONTENT_TYPE,
        kafka_topic=settings.heartbeat_delta_topic,
        shard_count=settings.heartbeat_heartbeat_shard_count,
        refresh_emit_interval_seconds=settings.heartbeat_refresh_emit_interval_seconds,
        delta_retention_hours=settings.heartbeat_delta_retention_hours,
        current_published_seq_by_shard={s: seq_to_str(v) for s, v in published_seq.items()},
    )


async def stream_snapshot(
    redis: Redis,
    exporter: SnapshotExporter,
) -> AsyncIterator[bytes]:
    """Delta log 健康检查后，委托 exporter.stream() 产出 NDJSON 字节流。

    使用方式（api.py）：
        StreamingResponse(stream_snapshot(redis, exporter), media_type="application/x-ndjson")

    Args:
        redis: Redis 客户端。
        exporter: SnapshotExporter 单例（通过 get_snapshot_exporter() 获取）。

    Yields:
        NDJSON 行字节（以 \\n 结尾）。

    Raises:
        DeltaLogUnhealthyError: outbox 积压超限（503）。
        SnapshotUnavailableError: Redis 连接异常（P1-4）。
    """
    await _ensure_delta_log_healthy(redis)

    try:
        async for chunk in exporter.stream(redis):
            yield chunk
    except (RedisConnectionError, RedisTimeoutError) as exc:
        raise SnapshotUnavailableError(str(exc)) from exc


async def _ensure_delta_log_healthy(redis: Redis) -> None:
    """检查所有分片的 outbox_publish_lag_ms，超限时抛出 DeltaLogUnhealthyError（P1-2，8-7）。

    使用 store.outbox_publish_lag_ms（非旧 PEL-only 函数），覆盖两种积压来源。

    Args:
        redis: Redis 客户端。

    Raises:
        DeltaLogUnhealthyError: 任意分片积压超过 relay_max_publish_lag_seconds * 1000 ms。
    """
    max_lag_ms = settings.heartbeat_relay_max_publish_lag_seconds * 1000
    for shard in all_shard_ids():
        lag_ms = await store.outbox_publish_lag_ms(redis, shard)
        if lag_ms is not None and lag_ms > max_lag_ms:
            raise DeltaLogUnhealthyError(f"shard {shard} outbox lag {lag_ms}ms exceeds threshold {max_lag_ms}ms")
