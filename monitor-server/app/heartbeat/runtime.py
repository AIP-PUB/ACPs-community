"""Heartbeat 模块 — 配置校验与生命周期装配（C-CONF-1）。

本文件包含：
- validate_heartbeat_config(): 静态配置校验（Step 3 提前落地）
- HeartbeatRuntime: 完整生命周期装配（Step 10）
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog

from app.core.config import settings
from app.heartbeat.exception import HeartbeatConfigError


def validate_heartbeat_config() -> None:
    """C-CONF-1：校验 Heartbeat 配置全量约束（§8 校验清单）。

    两条 Kafka 分区数校验需要连上 Kafka，分别放在 HeartbeatWriter.start() 与
    HeartbeatRelay.start() 中执行。本函数只校验不依赖外部连接的静态约束。

    Raises:
        HeartbeatConfigError: 配置非法，附带具体键名与原因（逐条 message）。
    """
    errors: list[str] = []

    refresh = settings.heartbeat_refresh_emit_interval_seconds
    silence = settings.heartbeat_silence_threshold_seconds
    evict = settings.heartbeat_evict_after_seconds

    # C-TIME-2 全序：refresh < silence < evict
    if refresh >= silence:
        errors.append(
            f"heartbeat.refresh_emit_interval_seconds ({refresh}) must be < "
            f"heartbeat.silence_threshold_seconds ({silence})"
        )
    if silence >= evict:
        errors.append(
            f"heartbeat.silence_threshold_seconds ({silence}) must be < heartbeat.evict_after_seconds ({evict})"
        )

    # 各正数约束
    for name, value in [
        ("heartbeat_shard_count", settings.heartbeat_heartbeat_shard_count),
        ("silence_threshold_seconds", silence),
        ("evict_after_seconds", evict),
        ("refresh_emit_interval_seconds", refresh),
        ("silent_scan_interval_seconds", settings.heartbeat_silent_scan_interval_seconds),
        ("evict_scan_interval_seconds", settings.heartbeat_evict_scan_interval_seconds),
        ("scan_batch_size", settings.heartbeat_scan_batch_size),
        ("scan_lock_ttl_seconds", settings.heartbeat_scan_lock_ttl_seconds),
        ("in_list_max", settings.heartbeat_in_list_max),
        ("silence_top_default_n", settings.heartbeat_silence_top_default_n),
        ("silence_top_max_n", settings.heartbeat_silence_top_max_n),
        ("silence_top_shard_fetch_size", settings.heartbeat_silence_top_shard_fetch_size),
        ("input_partition_count", settings.heartbeat_input_partition_count),
        ("outbox_max_len", settings.heartbeat_outbox_max_len),
        ("relay_published_seq_batch_size", settings.heartbeat_relay_published_seq_batch_size),
        ("snapshot_chunk_size", settings.heartbeat_snapshot_chunk_size),
        ("snapshot_max_enumeration_seconds", settings.heartbeat_snapshot_max_enumeration_seconds),
        ("metrics_log_interval_seconds", settings.heartbeat_metrics_log_interval_seconds),
    ]:
        if value <= 0:
            errors.append(f"heartbeat.{name} must be > 0, got {value}")

    # 非负约束
    for name, value in [
        ("relay_max_publish_lag_seconds", settings.heartbeat_relay_max_publish_lag_seconds),
        ("snapshot_max_alive_rows_per_s", settings.heartbeat_snapshot_max_alive_rows_per_s),
    ]:
        if value < 0:
            errors.append(f"heartbeat.{name} must be >= 0, got {value}")

    # summary_buckets_seconds 严格递增
    buckets = settings.heartbeat_summary_buckets_seconds
    if len(buckets) == 0:
        errors.append("heartbeat.summary_buckets_seconds must not be empty")
    else:
        for i in range(1, len(buckets)):
            if buckets[i] <= buckets[i - 1]:
                errors.append(
                    f"heartbeat.summary_buckets_seconds must be strictly increasing; "
                    f"buckets[{i}]={buckets[i]} <= buckets[{i - 1}]={buckets[i - 1]}"
                )
                break

    # silence_top_shard_fetch_size >= silence_top_max_n
    fetch_size = settings.heartbeat_silence_top_shard_fetch_size
    max_n = settings.heartbeat_silence_top_max_n
    if fetch_size < max_n:
        errors.append(
            f"heartbeat.silence_top_shard_fetch_size ({fetch_size}) must be >= heartbeat.silence_top_max_n ({max_n})"
        )

    # writer_watermark_stale_after_ms > writer_watermark_flush_interval_ms
    stale = settings.heartbeat_writer_watermark_stale_after_ms
    flush = settings.heartbeat_writer_watermark_flush_interval_ms
    if flush <= 0:
        errors.append(f"heartbeat.writer_watermark_flush_interval_ms must be > 0, got {flush}")
    if stale <= 0:
        errors.append(f"heartbeat.writer_watermark_stale_after_ms must be > 0, got {stale}")
    if flush > 0 and stale > 0 and stale <= flush:
        errors.append(
            f"heartbeat.writer_watermark_stale_after_ms ({stale}) must be > "
            f"heartbeat.writer_watermark_flush_interval_ms ({flush})"
        )

    if errors:
        msg = "Heartbeat 配置校验失败：\n" + "\n".join(f"  - {e}" for e in errors)
        raise HeartbeatConfigError(msg)


logger = structlog.get_logger(__name__)


class HeartbeatRuntime:
    """Heartbeat 模块生命周期装配器（§3.7 / §5.5）。

    负责：
    1. validate_heartbeat_config()（静态约束校验）
    2. ensure_functions_loaded()（Redis Functions 上线）
    3. 启动 HeartbeatWriter + HeartbeatReconciler 后台任务
    4. sync_enabled → 启动 HeartbeatRelay 后台任务
    5. 启动 metrics_log_loop 后台任务

    stop() 逆序 cancel + suppress(CancelledError)；
    writer 停止前先执行 _flush_watermarks（保存水位到 Redis）。
    """

    def __init__(self) -> None:
        self._writer: object = None
        self._reconciler: object = None
        self._relay: object = None
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        """启动所有 Heartbeat 后台组件。

        Raises:
            HeartbeatConfigError: 静态配置校验失败，进程必须拒绝启动。
        """
        from app.core.redis_client import get_redis
        from app.heartbeat.functions import ensure_functions_loaded
        from app.heartbeat.metrics import metrics_log_loop
        from app.heartbeat.reconciler import HeartbeatReconciler
        from app.heartbeat.relay import HeartbeatRelay
        from app.heartbeat.writer import HeartbeatWriter

        validate_heartbeat_config()

        redis = get_redis()
        await ensure_functions_loaded(redis)

        writer = HeartbeatWriter(redis)
        self._writer = writer
        await writer.start()
        self._tasks.append(asyncio.create_task(writer.run(), name="heartbeat-writer"))

        reconciler = HeartbeatReconciler(redis)
        self._reconciler = reconciler
        self._tasks.append(asyncio.create_task(reconciler.run(), name="heartbeat-reconciler"))

        if settings.heartbeat_sync_enabled:
            relay = HeartbeatRelay(redis)
            self._relay = relay
            await relay.start()
            self._tasks.append(asyncio.create_task(relay.run(), name="heartbeat-relay"))

        interval = settings.heartbeat_metrics_log_interval_seconds
        self._tasks.append(asyncio.create_task(metrics_log_loop(interval), name="heartbeat-metrics"))

        logger.info(
            "HeartbeatRuntime started",
            sync_enabled=settings.heartbeat_sync_enabled,
            shard_count=settings.heartbeat_heartbeat_shard_count,
        )

    async def stop(self) -> None:
        """逆序停止所有 Heartbeat 后台组件。

        writer 停止前先执行 _flush_watermarks（确保最新水位持久化到 Redis）。
        cancel + suppress(CancelledError) 保证优雅退出（同 main.py Audit 先例）。
        """
        from app.heartbeat.writer import HeartbeatWriter

        # writer 停止前刷新水位
        if isinstance(self._writer, HeartbeatWriter):
            with contextlib.suppress(Exception):
                await self._writer._flush_watermarks()

        # 逆序 cancel tasks
        for task in reversed(self._tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

        # 停止 relay producer
        if self._relay is not None:
            from app.heartbeat.relay import HeartbeatRelay

            if isinstance(self._relay, HeartbeatRelay):
                with contextlib.suppress(Exception):
                    await self._relay.stop()

        logger.info("HeartbeatRuntime stopped")
