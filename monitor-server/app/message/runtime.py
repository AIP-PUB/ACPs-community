"""app/message/runtime.py — 配置校验与组件装配（设计 §6.23）。

对齐 access.runtime：
  - `validate_message_config()` 跨键约束校验（模块函数，无参数）
  - `MessageRuntime` 类（__init__ / start / stop）
"""

from __future__ import annotations

import asyncio
import contextlib
from asyncio import Task
from typing import Any

import structlog

from app.core.config import settings
from app.message import store
from app.message.exception import MessageConfigError

logger = structlog.get_logger(__name__)


def validate_message_config() -> None:
    """启动校验清单（设计 §6.23 / C-MESSAGE-WRITE-2 / C-MESSAGE-RETENTION-2/5）。

    失败 raise MessageConfigError(errors=[...])，进程拒绝启动。
    单键值域校验已在 config property 即时 raise；此处聚合跨键约束。
    """
    s = settings
    errors: list[str] = []

    if s.message_dedup_window_seconds < s.message_kafka_retention_seconds:
        errors.append(
            f"message_dedup_window_seconds ({s.message_dedup_window_seconds}) "
            f"must be >= message_kafka_retention_seconds ({s.message_kafka_retention_seconds}) "
            "(C-MESSAGE-WRITE-2)"
        )

    if s.message_lifecycle_retention_days < s.message_raw_retention_days:
        errors.append(
            f"message_lifecycle_retention_days ({s.message_lifecycle_retention_days}) "
            f"must be >= message_raw_retention_days ({s.message_raw_retention_days}) "
            "(C-MESSAGE-RETENTION-2)"
        )

    if s.message_destination_state_retention_days < s.message_raw_retention_days:
        errors.append(
            f"message_destination_state_retention_days ({s.message_destination_state_retention_days}) "
            f"must be >= message_raw_retention_days ({s.message_raw_retention_days}) "
            "(C-MESSAGE-RETENTION-2)"
        )

    if s.message_destination_stats_retention_days < s.message_raw_retention_days:
        errors.append(
            f"message_destination_stats_retention_days ({s.message_destination_stats_retention_days}) "
            f"must be >= message_raw_retention_days ({s.message_raw_retention_days}) "
            "(C-MESSAGE-RETENTION-2)"
        )

    # overlap > interval は警告のみ（testing.toml は短い interval を使う設計）

    if errors:
        raise MessageConfigError(errors)


class MessageRuntime:
    """Message 模块运行时：启动 DDL bootstrap + 后台任务（Writer / Compactors / Collector）。"""

    def __init__(self) -> None:
        self._tasks: list[Task[None]] = []
        self._writer: Any | None = None
        self._archive: Any | None = None

    @property
    def is_writer_enabled(self) -> bool:
        return settings.message_writer_enabled

    @property
    def is_lifecycle_enabled(self) -> bool:
        return settings.message_reliability_enabled

    @property
    def is_throughput_enabled(self) -> bool:
        return settings.message_destination_enabled

    @property
    def is_destination_enabled(self) -> bool:
        return settings.message_destination_enabled

    async def start(self) -> None:
        """启动 Message 模块所有后台任务。

        顺序：
        1. validate_message_config()（失败 fail-fast）
        2. store.ensure_message_schema()（DDL bootstrap，幂等）
        3. APP_ENV=testing → return（跳过后台 IO 任务）
        4. 按 feature flag 条件启动 Writer / Compactors / Collector / metrics_log_loop / archive
        """
        validate_message_config()
        await store.ensure_message_schema()

        if settings.app_env == "testing":
            logger.info("message_runtime.start.skipped", reason="app_env=testing")
            return

        from app.core.redis_client import get_redis

        redis = get_redis()

        if self.is_writer_enabled:
            from app.message.writer import MessageWriter

            writer = MessageWriter(redis)
            await writer.start()
            self._writer = writer
            self._tasks.append(asyncio.create_task(writer.run(), name="message-writer"))

        if self.is_lifecycle_enabled:
            from app.message.lifecycle_compactor import LifecycleCompactor

            lc = LifecycleCompactor(redis)
            self._tasks.append(asyncio.create_task(lc.run(), name="message-lifecycle-compactor"))

        if self.is_throughput_enabled:
            from app.message.throughput_compactor import ThroughputCompactor

            tc = ThroughputCompactor(redis)
            self._tasks.append(asyncio.create_task(tc.run(), name="message-throughput-compactor"))

        if settings.message_state_collector_enabled:
            from app.message.destination_source import build_destination_source
            from app.message.state_collector import DestinationStateCollector

            source = build_destination_source(settings)
            collector = DestinationStateCollector(redis, source)
            self._tasks.append(asyncio.create_task(collector.run(), name="message-state-collector"))

        from app.message.metrics import metrics_log_loop

        self._tasks.append(
            asyncio.create_task(
                metrics_log_loop(settings.message_metrics_log_interval_seconds),
                name="message-metrics",
            )
        )

        if settings.message_archive_enabled:
            from app.message.archive import MessageArchiveTask

            archive_task = MessageArchiveTask(redis)
            self._archive = archive_task
            self._tasks.append(asyncio.create_task(archive_task.run(), name="message-archive"))
            logger.info("message_runtime.archive_enabled")

        logger.info("message_runtime.started", task_count=len(self._tasks))

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
                logger.warning("message_runtime.writer_stop_error", exc_info=True)
            self._writer = None

        if self._archive is not None:
            self._archive.stop()
            self._archive = None

        logger.info("message_runtime.stopped")


__all__ = [
    "MessageRuntime",
    "validate_message_config",
]
