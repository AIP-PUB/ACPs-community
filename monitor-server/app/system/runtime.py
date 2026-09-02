"""app/system/runtime.py — 配置校验 + schema bootstrap + 生命周期装配。

validate_system_config() 是跨键约束校验单一入口；
SystemRuntime 封装启停（启动时 bootstrap 索引模板/ISM）。对齐 access/metrics runtime。
"""

from __future__ import annotations

import asyncio
import contextlib
from asyncio import Task
from typing import Any

import structlog

from app.system.exception import SystemConfigError

logger = structlog.get_logger(__name__)


def validate_system_config() -> None:
    """启动校验清单（设计 §7）。失败 raise SystemConfigError，进程拒绝启动。"""
    from app.core.config import settings

    s = settings
    errors: list[str] = []

    if s.system_bulk_index_batch_interval_seconds <= 0:
        errors.append("system_bulk_index_batch_interval_seconds must be > 0")
    if s.system_bulk_index_batch_max_docs <= 0:
        errors.append("system_bulk_index_batch_max_docs must be > 0")
    if s.system_event_hot_retention_days <= 0:
        errors.append("system_event_hot_retention_days must be > 0")
    if s.system_event_warm_retention_days < s.system_event_hot_retention_days:
        errors.append(
            f"system_event_warm_retention_days ({s.system_event_warm_retention_days}) "
            f"must be >= system_event_hot_retention_days ({s.system_event_hot_retention_days})"
        )
    if s.system_archive_retention_days < s.system_event_warm_retention_days:
        errors.append(
            f"system_archive_retention_days ({s.system_archive_retention_days}) "
            f"must be >= system_event_warm_retention_days ({s.system_event_warm_retention_days})"
        )
    if s.system_lagging_threshold_ms <= 0:
        errors.append("system_lagging_threshold_ms must be > 0")
    if s.system_query_timeout_seconds <= 0:
        errors.append("system_query_timeout_seconds must be > 0")
    if s.system_keyword_min_length <= 0:
        errors.append("system_keyword_min_length must be > 0")
    if s.system_search_text_max_length <= 0:
        errors.append("system_search_text_max_length must be > 0")
    if s.system_freshness_reorder_margin_ms < 0:
        errors.append("system_freshness_reorder_margin_ms must be >= 0")
    if s.system_keyword_only_max_window_seconds <= 0:
        errors.append("system_keyword_only_max_window_seconds must be > 0")
    if s.system_lagging_response_mode not in {"503", "partial"}:
        errors.append(
            f"system_lagging_response_mode must be '503' or 'partial', got {s.system_lagging_response_mode!r}"
        )
    if s.system_index_number_of_shards <= 0:
        errors.append("system_index_number_of_shards must be > 0")
    if s.system_index_number_of_replicas < 0:
        errors.append("system_index_number_of_replicas must be >= 0")
    if not s.system_pit_keep_alive or not s.system_pit_keep_alive.strip():
        errors.append("system_pit_keep_alive must be non-empty")

    if errors:
        raise SystemConfigError(errors)


class SystemRuntime:
    """System 模块运行时：bootstrap + Writer + metrics_log_loop + maintenance（可选）。"""

    def __init__(self) -> None:
        self._tasks: list[Task[Any]] = []
        self._writer: Any | None = None
        self._maintenance: Any | None = None

    async def start(self) -> None:
        """启动 System 模块所有后台任务。

        1. validate_system_config()（失败则进程拒绝启动）
        2. store.ensure_system_schema()（模板+ISM bootstrap，幂等）
        3. APP_ENV=testing 跳过后台 IO 任务
        4. SystemWriter 启动（system_writer_enabled=true）
        5. metrics_log_loop 后台任务
        6. SystemMaintenanceTask（system_archive_enabled=true）
        """
        from app.core.config import settings

        validate_system_config()

        from app.system import store

        await store.ensure_system_schema(
            number_of_shards=settings.system_index_number_of_shards,
            number_of_replicas=settings.system_index_number_of_replicas,
            hot_days=settings.system_event_hot_retention_days,
            warm_days=settings.system_event_warm_retention_days,
            archive_days=settings.system_archive_retention_days,
        )

        if settings.app_env == "testing":
            logger.info("system_runtime.start.skipped", reason="app_env=testing")
            return

        # SystemWriter
        if settings.system_writer_enabled:
            from app.core.redis_client import get_redis
            from app.system.writer import SystemWriter

            writer = SystemWriter(get_redis())
            await writer.start()
            self._writer = writer
            self._tasks.append(asyncio.create_task(writer.run(), name="system-writer"))

        # metrics_log_loop
        from app.system.metrics import metrics_log_loop

        self._tasks.append(
            asyncio.create_task(
                metrics_log_loop(settings.system_metrics_log_interval_seconds),
                name="system-metrics",
            )
        )

        # Maintenance（可选）
        if settings.system_archive_enabled:
            from app.system.maintenance import SystemMaintenanceTask

            maintenance_task = SystemMaintenanceTask()
            self._maintenance = maintenance_task
            self._tasks.append(asyncio.create_task(maintenance_task.run(), name="system-maintenance"))
            logger.info("system_runtime.maintenance_enabled")

        logger.info("system_runtime.started", task_count=len(self._tasks))

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
                logger.warning("system_runtime.writer_stop_error", exc_info=True)
            self._writer = None

        if self._maintenance is not None:
            self._maintenance.stop()
            self._maintenance = None

        from app.core.opensearch_client import close_opensearch_client

        await close_opensearch_client()
        logger.info("system_runtime.stopped")


__all__ = ["SystemRuntime", "validate_system_config"]
