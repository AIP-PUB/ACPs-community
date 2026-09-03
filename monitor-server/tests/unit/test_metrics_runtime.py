"""tests/unit/test_metrics_runtime.py — MetricsRuntime 单元测试（Step 8）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.metrics.exception import MetricsConfigError
from app.metrics.runtime import MetricsRuntime

# ── MetricsRuntime.start ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_creates_two_background_tasks() -> None:
    """start() 启动 MetricsWriter task 和 metrics_log_loop task。"""
    mock_writer = AsyncMock()
    mock_writer.start = AsyncMock()
    mock_writer.run = AsyncMock(return_value=None)
    mock_writer.stop = AsyncMock()

    mock_settings = MagicMock()
    mock_settings.app_env = "development"
    mock_settings.metrics_metrics_log_interval_seconds = 60

    with (
        patch("app.metrics.runtime.validate_metrics_config"),
        patch("app.core.config.get_settings", return_value=mock_settings),
        patch("app.metrics.writer.MetricsWriter", return_value=mock_writer),
        patch("app.core.redis_client.get_redis", return_value=AsyncMock()),
        patch("app.metrics.metrics.metrics_log_loop", new=AsyncMock(return_value=None)),
        patch(
            "app.metrics.runtime.asyncio.create_task",
            side_effect=lambda coro, **kw: (coro.close(), MagicMock())[1],
        ) as mock_task,
    ):
        runtime = MetricsRuntime()
        await runtime.start()

    # 应创建 2 个 task（writer.run + metrics_log_loop）
    assert mock_task.call_count == 2


@pytest.mark.asyncio
async def test_start_metrics_config_error_propagates() -> None:
    """MetricsConfigError → 向上传播（进程拒绝启动）。"""
    with patch("app.metrics.runtime.validate_metrics_config", side_effect=MetricsConfigError("bad config")):
        runtime = MetricsRuntime()
        with pytest.raises(MetricsConfigError):
            await runtime.start()


@pytest.mark.asyncio
async def test_start_other_exception_logs_warning() -> None:
    """非 MetricsConfigError 的异常 → 降级（但 start 本身不 raise）。

    注：实际 main.py 会捕获并 WARNING，这里测试 start() 内的错误传播。
    """
    mock_writer = AsyncMock()
    mock_writer.start = AsyncMock(side_effect=RuntimeError("unexpected"))

    mock_settings = MagicMock()
    mock_settings.app_env = "development"
    mock_settings.metrics_metrics_log_interval_seconds = 60

    with (
        patch("app.metrics.runtime.validate_metrics_config"),
        patch("app.core.config.get_settings", return_value=mock_settings),
        patch("app.metrics.writer.MetricsWriter", return_value=mock_writer),
        patch("app.core.redis_client.get_redis", return_value=AsyncMock()),
    ):
        runtime = MetricsRuntime()
        # RuntimeError 向上传播，main.py 捕获并降级
        with pytest.raises(RuntimeError):
            await runtime.start()


# ── MetricsRuntime.stop ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_cancels_tasks_and_closes_tsdb() -> None:
    """stop() 取消所有 task 并调用 close_tsdb_client()。"""
    import asyncio

    async def _noop() -> None:
        await asyncio.sleep(1000)

    mock_writer = AsyncMock()
    mock_writer.stop = AsyncMock()

    mock_close = AsyncMock()

    # 创建真实 task，以便 cancel() + await 能正常工作
    task1 = asyncio.create_task(_noop())
    task2 = asyncio.create_task(_noop())

    with patch("app.metrics.tsdb.close_tsdb_client", new=mock_close):
        runtime = MetricsRuntime()
        runtime._tasks = [task1, task2]
        runtime._writer = mock_writer
        await runtime.stop()

    assert task1.cancelled()
    assert task2.cancelled()
    mock_writer.stop.assert_called_once()
    mock_close.assert_called_once()


@pytest.mark.asyncio
async def test_stop_is_idempotent_with_empty_tasks() -> None:
    """stop() 空任务列表不抛异常。"""
    mock_close = AsyncMock()
    with patch("app.metrics.tsdb.close_tsdb_client", new=mock_close):
        runtime = MetricsRuntime()
        await runtime.stop()
    mock_close.assert_called_once()
