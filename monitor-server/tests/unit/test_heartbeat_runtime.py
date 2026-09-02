"""tests/unit/test_heartbeat_runtime.py — HeartbeatRuntime 生命周期单元测试。

覆盖：
- start() 创建 writer / reconciler task（sync_disabled 时不创建 relay task）
- start() 创建 relay task（sync_enabled）
- start() 在 validate_heartbeat_config() 失败时直接抛出
- stop() 逆序 cancel tasks
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.heartbeat.exception import HeartbeatConfigError
from app.heartbeat.runtime import HeartbeatRuntime


class _FakeTask:
    """asyncio.Task 的简单模拟（cancel/await 都是 no-op）。"""

    cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def __await__(self) -> object:
        return iter([])


def _make_mock_component() -> MagicMock:
    """构造一个 start/run/stop 均为 AsyncMock 的组件 mock。"""
    m = MagicMock()
    m.start = AsyncMock()
    m.run = AsyncMock()
    m.stop = AsyncMock()
    return m


class TestHeartbeatRuntimeStart:
    @pytest.mark.asyncio
    async def test_start_creates_writer_and_reconciler_tasks(self) -> None:
        """sync_disabled：start() 产生 writer + reconciler + metrics 共 3 个 task。"""
        runtime = HeartbeatRuntime()

        mock_writer = _make_mock_component()
        mock_reconciler = _make_mock_component()

        with (
            patch("app.heartbeat.runtime.validate_heartbeat_config"),
            patch("app.heartbeat.runtime.settings") as mock_settings,
            patch("app.heartbeat.runtime.HeartbeatRuntime.start.__wrapped__", None, create=True),
            patch("app.core.redis_client.get_redis", return_value=MagicMock()),
            patch("app.heartbeat.functions.ensure_functions_loaded", new_callable=AsyncMock),
            patch("app.heartbeat.writer.HeartbeatWriter", return_value=mock_writer),
            patch("app.heartbeat.reconciler.HeartbeatReconciler", return_value=mock_reconciler),
            patch("app.heartbeat.metrics.metrics_log_loop", new_callable=AsyncMock),
        ):
            mock_settings.heartbeat_sync_enabled = False
            mock_settings.heartbeat_metrics_log_interval_seconds = 60
            mock_settings.heartbeat_heartbeat_shard_count = 1

            tasks_created: list[str] = []

            def fake_create_task(coro: object, *, name: str = "") -> _FakeTask:
                tasks_created.append(name)
                if asyncio.iscoroutine(coro):
                    coro.close()
                return _FakeTask()

            with patch("asyncio.create_task", side_effect=fake_create_task):
                await runtime.start()

        assert "heartbeat-writer" in tasks_created
        assert "heartbeat-reconciler" in tasks_created
        assert "heartbeat-metrics" in tasks_created
        assert "heartbeat-relay" not in tasks_created

    @pytest.mark.asyncio
    async def test_start_creates_relay_task_when_sync_enabled(self) -> None:
        """sync_enabled：start() 额外产生 relay task，共 4 个 task。"""
        runtime = HeartbeatRuntime()

        mock_writer = _make_mock_component()
        mock_reconciler = _make_mock_component()
        mock_relay = _make_mock_component()

        with (
            patch("app.heartbeat.runtime.validate_heartbeat_config"),
            patch("app.heartbeat.runtime.settings") as mock_settings,
            patch("app.core.redis_client.get_redis", return_value=MagicMock()),
            patch("app.heartbeat.functions.ensure_functions_loaded", new_callable=AsyncMock),
            patch("app.heartbeat.writer.HeartbeatWriter", return_value=mock_writer),
            patch("app.heartbeat.reconciler.HeartbeatReconciler", return_value=mock_reconciler),
            patch("app.heartbeat.relay.HeartbeatRelay", return_value=mock_relay),
            patch("app.heartbeat.metrics.metrics_log_loop", new_callable=AsyncMock),
        ):
            mock_settings.heartbeat_sync_enabled = True
            mock_settings.heartbeat_metrics_log_interval_seconds = 60
            mock_settings.heartbeat_heartbeat_shard_count = 1

            tasks_created: list[str] = []

            def fake_create_task(coro: object, *, name: str = "") -> _FakeTask:
                tasks_created.append(name)
                if asyncio.iscoroutine(coro):
                    coro.close()
                return _FakeTask()

            with patch("asyncio.create_task", side_effect=fake_create_task):
                await runtime.start()

        assert "heartbeat-relay" in tasks_created
        assert len(tasks_created) == 4

    @pytest.mark.asyncio
    async def test_start_raises_on_config_error(self) -> None:
        """validate_heartbeat_config() 抛出 HeartbeatConfigError 时 start() 传播异常。"""
        runtime = HeartbeatRuntime()
        with (
            patch(
                "app.heartbeat.runtime.validate_heartbeat_config",
                side_effect=HeartbeatConfigError("config invalid"),
            ),
            pytest.raises(HeartbeatConfigError, match="config invalid"),
        ):
            await runtime.start()


class TestHeartbeatRuntimeStop:
    @pytest.mark.asyncio
    async def test_stop_cancels_all_tasks(self) -> None:
        """stop() 取消所有 _tasks 中的 task。"""
        runtime = HeartbeatRuntime()
        tasks = [_FakeTask(), _FakeTask(), _FakeTask()]
        runtime._tasks = tasks  # type: ignore[assignment]

        await runtime.stop()

        assert all(t.cancelled for t in tasks)
        assert runtime._tasks == []

    @pytest.mark.asyncio
    async def test_stop_flushes_writer_watermarks(self) -> None:
        """stop() 对 HeartbeatWriter 调用 _flush_watermarks。"""
        from app.heartbeat.writer import HeartbeatWriter

        runtime = HeartbeatRuntime()
        mock_writer = MagicMock(spec=HeartbeatWriter)
        mock_writer._flush_watermarks = AsyncMock()
        runtime._writer = mock_writer

        await runtime.stop()

        mock_writer._flush_watermarks.assert_awaited_once()
