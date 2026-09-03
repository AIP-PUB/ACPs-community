"""app/metrics/runtime.py — Metrics 模块配置校验与生命周期装配。

实现设计 §6.18 runtime.py。
validate_metrics_config() 是所有交叉键约束校验的单一入口。
MetricsRuntime 封装启动 / 停止生命周期（无 Reconciler / Relay，metrics 无 Sync 平面）。
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from asyncio import Task

import structlog

from app.core.config import get_settings
from app.metrics.exception import MetricsConfigError
from app.metrics.writer import MetricsWriter

logger = structlog.get_logger(__name__)

# ISO 8601 Duration 最简正则（仅校验可解析性，不计算值）
_ISO8601_DURATION_RE = re.compile(r"^P(?:\d+Y)?(?:\d+M)?(?:\d+W)?(?:\d+D)?(?:T(?:\d+H)?(?:\d+M)?(?:\d+(?:\.\d+)?S)?)?$")


def _valid_iso_duration(value: str) -> bool:
    """粗校 ISO 8601 Duration 字符串是否合法（非空且匹配基本格式）。"""
    if not value or value == "P":
        return False
    return bool(_ISO8601_DURATION_RE.match(value))


def validate_metrics_config() -> None:
    """校验 Metrics 所有交叉键约束（设计 §7 启动校验清单）。

    校验失败时 raise MetricsConfigError，详细信息包含违规键名，
    调用方（MetricsRuntime.start）应将此错误视为启动失败（进程拒绝启动）。

    Raises:
        MetricsConfigError: 存在无效配置项。
    """
    s = get_settings()

    errors: list[str] = []

    def _check(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    # Writer 批次配置
    _check(s.metrics_writer_poll_timeout_ms > 0, "metrics.writer_poll_timeout_ms must be > 0")
    _check(s.metrics_remote_write_batch_interval_seconds > 0, "metrics.remote_write_batch_interval_seconds must be > 0")
    _check(s.metrics_remote_write_batch_max_samples > 0, "metrics.remote_write_batch_max_samples must be > 0")

    # 缓存
    _check(s.metrics_snapshot_ttl_seconds > 0, "metrics.snapshot_ttl_seconds must be > 0")
    _check(s.metrics_snapshot_index_scan_batch_size > 0, "metrics.snapshot_index_scan_batch_size must be > 0")
    _check(s.metrics_dedupe_ttl_seconds > 0, "metrics.dedupe_ttl_seconds must be > 0")

    # 保留窗口
    _check(s.metrics_raw_retention_days > 0, "metrics.raw_retention_days must be > 0")
    _check(
        s.metrics_downsample_retention_days > s.metrics_raw_retention_days,
        "metrics.downsample_retention_days must be > raw_retention_days",
    )

    # 查询控制
    _check(s.metrics_lagging_threshold_ms > 0, "metrics.lagging_threshold_ms must be > 0")
    _check(s.metrics_max_points_per_series > 0, "metrics.max_points_per_series must be > 0")
    _check(s.metrics_ranking_max_top_n > 0, "metrics.ranking_max_top_n must be > 0")
    _check(s.metrics_slo_max_rules > 0, "metrics.slo_max_rules must be > 0")
    _check(s.metrics_query_timeout_seconds > 0, "metrics.query_timeout_seconds must be > 0")

    # capacity 阈值（交叉键校验，单键校验已在 property 层）
    try:
        active_thr = s.metrics_capacity_default_active_ratio_threshold
        _check(0 < active_thr <= 1, "metrics.capacity_default_active_ratio_threshold must be in (0, 1]")
    except ValueError as exc:
        errors.append(str(exc))

    try:
        queue_thr = s.metrics_capacity_default_queue_ratio_threshold
        _check(0 < queue_thr <= 1, "metrics.capacity_default_queue_ratio_threshold must be in (0, 1]")
    except ValueError as exc:
        errors.append(str(exc))

    # ISO 8601 Duration 可解析性
    for key, attr in [
        ("metrics.snapshot_fallback_lookback", "metrics_snapshot_fallback_lookback"),
        ("metrics.capacity_default_lookback", "metrics_capacity_default_lookback"),
    ]:
        val = getattr(s, attr, "")
        _check(_valid_iso_duration(val), f"{key} must be a valid ISO 8601 Duration (e.g. PT10M), got: {val!r}")

    # lagging_response_mode
    try:
        mode = s.metrics_lagging_response_mode
        _check(mode in {"503", "partial"}, f"metrics.lagging_response_mode must be '503' or 'partial', got: {mode!r}")
    except ValueError as exc:
        errors.append(str(exc))

    # 可观测性
    _check(s.metrics_metrics_log_interval_seconds > 0, "metrics.metrics_log_interval_seconds must be > 0")

    if errors:
        bullet = "\n  - ".join(errors)
        raise MetricsConfigError(f"Metrics configuration is invalid:\n  - {bullet}")


# ── MetricsRuntime ─────────────────────────────────────────────────────────────


class MetricsRuntime:
    """Metrics 模块运行时：启动 Writer + metrics_log_loop，持有 Task 句柄列表。

    无 Reconciler / Relay（metrics 无 Sync 平面，设计 §1.2）。
    """

    def __init__(self) -> None:
        self._tasks: list[Task[None]] = []
        self._writer: MetricsWriter | None = None

    async def start(self) -> None:
        """启动 Metrics 模块所有后台任务。

        1. validate_metrics_config()（失败则进程拒绝启动）
        2. MetricsWriter 启动
        3. metrics_log_loop 启动

        Raises:
            MetricsConfigError: 配置校验失败。
        """
        from app.core.config import get_settings
        from app.core.redis_client import get_redis
        from app.metrics.writer import MetricsWriter

        # 配置校验（失败则让异常上浮，拒绝启动）
        validate_metrics_config()

        s = get_settings()

        # APP_ENV=testing 时跳过后台 IO 任务（同 heartbeat 先例）
        if s.app_env == "testing":
            logger.info("metrics_runtime.start.skipped", reason="app_env=testing")
            return

        # 启动 Writer
        writer = MetricsWriter(get_redis())
        await writer.start()
        self._writer = writer
        self._tasks.append(asyncio.create_task(writer.run(), name="metrics-writer"))

        # 启动 metrics_log_loop
        from app.metrics.metrics import metrics_log_loop

        self._tasks.append(
            asyncio.create_task(
                metrics_log_loop(s.metrics_metrics_log_interval_seconds),
                name="metrics-metrics",
            )
        )

        logger.info("metrics_runtime.started", task_count=len(self._tasks))

    async def stop(self) -> None:
        """停止所有后台任务（幂等）。"""
        from app.metrics.tsdb import close_tsdb_client

        # 逆序取消（Writer 先停，log_loop 后停）
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
                logger.warning("metrics_runtime.writer_stop_error", exc_info=True)
            self._writer = None

        await close_tsdb_client()

        logger.info("metrics_runtime.stopped")


__all__ = [
    "MetricsRuntime",
    "validate_metrics_config",
]
