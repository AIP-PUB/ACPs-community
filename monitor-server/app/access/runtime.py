"""app/access/runtime.py — 配置校验与生命周期装配（设计 §6.20）。

validate_access_config() 是所有交叉键约束的单一入口。
AccessRuntime 封装 ensure schema + 后台任务生命周期（D-4 阶段完整实现）。
"""

from __future__ import annotations

import asyncio
import contextlib
from asyncio import Task
from typing import TYPE_CHECKING, Any

import structlog

from app.access import store
from app.access.exception import AccessConfigError
from app.access.tables import TOPOLOGY_MV_DEFAULT_ERROR_THRESHOLD
from app.core.clickhouse_client import close_clickhouse_client
from app.core.config import get_settings, settings

if TYPE_CHECKING:
    from app.access.writer import AccessWriter

logger = structlog.get_logger(__name__)


def validate_access_config() -> None:
    """启动校验清单（设计 §7）。

    失败 raise AccessConfigError，进程拒绝启动。
    单键值域校验已在 config property 即时 raise；此处聚合 + 跨键关系校验。
    """
    s = settings
    errors: list[str] = []

    if s.access_insert_batch_interval_seconds <= 0:
        errors.append("access_insert_batch_interval_seconds must be > 0")

    if s.access_insert_batch_max_rows <= 0:
        errors.append("access_insert_batch_max_rows must be > 0")

    if s.access_raw_retention_days <= 0:
        errors.append("access_raw_retention_days must be > 0")

    if s.access_archive_retention_days < s.access_raw_retention_days:
        errors.append(
            f"access_archive_retention_days ({s.access_archive_retention_days}) "
            f"must be >= access_raw_retention_days ({s.access_raw_retention_days})"
        )

    if s.access_topology_retention_days < s.access_raw_retention_days:
        errors.append(
            f"access_topology_retention_days ({s.access_topology_retention_days}) "
            f"must be >= access_raw_retention_days ({s.access_raw_retention_days})"
        )

    if s.access_lagging_threshold_ms <= 0:
        errors.append("access_lagging_threshold_ms must be > 0")

    if s.access_query_timeout_seconds <= 0:
        errors.append("access_query_timeout_seconds must be > 0")

    if s.access_trace_max_spans <= 0:
        errors.append("access_trace_max_spans must be > 0")

    if s.access_slow_top_max_n <= 0:
        errors.append("access_slow_top_max_n must be > 0")

    if s.access_error_attribution_max_n <= 0:
        errors.append("access_error_attribution_max_n must be > 0")

    if s.access_dedup_window_hours < 1:
        errors.append("access_dedup_window_hours must be >= 1")

    if s.access_trace_max_duration_hours < 1:
        errors.append("access_trace_max_duration_hours must be >= 1")

    if s.access_lagging_response_mode not in {"503", "partial"}:
        errors.append(
            f"access_lagging_response_mode must be '503' or 'partial', got {s.access_lagging_response_mode!r}"
        )

    if errors:
        raise AccessConfigError(errors)

    # 非阻断警告：error_status_threshold 改值时拓扑 MV 固化阈值与运行时不一致
    if s.access_error_status_threshold != TOPOLOGY_MV_DEFAULT_ERROR_THRESHOLD:
        logger.warning(
            "access.error_status_threshold 与拓扑 MV 固化阈值不一致，建议重建拓扑 MV（DROP + CREATE）以统一口径",
            runtime_threshold=s.access_error_status_threshold,
            mv_baked_threshold=TOPOLOGY_MV_DEFAULT_ERROR_THRESHOLD,
        )
        from app.access.metrics import metrics as _metrics

        _metrics.inc("amp_access_mv_desync_total")


# ── AccessRuntime ─────────────────────────────────────────────────────────────


class AccessRuntime:
    """Access 模块运行时：启动 DDL bootstrap + Writer + metrics_log_loop + archive（可选），持有 Task 句柄列表。"""

    def __init__(self) -> None:
        self._tasks: list[Task[None]] = []
        self._writer: AccessWriter | None = None
        self._archive: Any | None = None  # AccessArchiveTask | None

    async def start(self) -> None:
        """启动 Access 模块所有后台任务。

        1. validate_access_config()（失败则进程拒绝启动）
        2. store.ensure_access_schema()（建表，幂等）
        3. APP_ENV=testing 跳过后台 IO 任务
        4. AccessWriter 启动
        5. metrics_log_loop 后台任务
        6. AccessArchiveTask（access_archive_enabled=true 时启动）

        Raises:
            AccessConfigError: 配置校验失败。
        """
        validate_access_config()

        s = get_settings()

        # DDL bootstrap（幂等；CH 不可达则上浮 → main 降级处理）
        await store.ensure_access_schema()

        if s.app_env == "testing":
            logger.info("access_runtime.start.skipped", reason="app_env=testing")
            return

        # 启动 AccessWriter
        from app.access.writer import AccessWriter
        from app.core.redis_client import get_redis

        writer = AccessWriter(get_redis())
        await writer.start()
        self._writer = writer
        self._tasks.append(asyncio.create_task(writer.run(), name="access-writer"))

        # 启动 metrics_log_loop
        from app.access.metrics import metrics_log_loop

        self._tasks.append(
            asyncio.create_task(
                metrics_log_loop(s.access_metrics_log_interval_seconds),
                name="access-metrics",
            )
        )

        # 启动 Parquet 冷归档后台任务（可选）
        if s.access_archive_enabled:
            from app.access.archive import AccessArchiveTask

            archive_task = AccessArchiveTask(get_redis())
            self._archive = archive_task
            self._tasks.append(asyncio.create_task(archive_task.run(), name="access-archive"))
            logger.info("access_runtime.archive_enabled")

        logger.info("access_runtime.started", task_count=len(self._tasks))

    async def stop(self) -> None:
        """停止所有后台任务（幂等）。"""
        for task in reversed(self._tasks):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        self._tasks.clear()

        if self._writer is not None:
            try:
                await self._writer.stop()
            except Exception:
                logger.warning("access_runtime.writer_stop_error", exc_info=True)
            self._writer = None

        if self._archive is not None:
            self._archive.stop()
            self._archive = None

        await close_clickhouse_client()

        logger.info("access_runtime.stopped")


__all__ = [
    "AccessRuntime",
    "validate_access_config",
]
