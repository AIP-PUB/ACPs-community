"""tests/unit/system/test_maintenance.py — maintenance.py 单元测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.system.maintenance import SystemMaintenanceTask, archive_expiring_indices


class TestSystemMaintenanceTask:
    @pytest.mark.asyncio
    async def test_run_once_calls_ensure_ism_attached(self) -> None:
        """run_once 调用 store.ensure_ism_attached()（mock store 断言被调用）。"""
        task = SystemMaintenanceTask()
        mock_ism = AsyncMock()
        with patch("app.system.store.ensure_ism_attached", mock_ism):
            await task.run_once()
        mock_ism.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_once_exception_does_not_propagate_from_run(self) -> None:
        """run() 内部异常不外抛杀循环（非致命，只告警）。"""
        task = SystemMaintenanceTask()

        call_count = 0

        async def raise_once() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated error")

        with patch.object(task, "run_once", side_effect=raise_once):
            # run is an infinite loop; stop it after first iteration
            task.stop()
            # run will exit quickly because stop_event is set
            with patch("app.core.config.settings") as mock_settings:
                mock_settings.system_archive_interval_seconds = 0
                await task.run()
        # No exception raised

    @pytest.mark.asyncio
    async def test_archive_expiring_indices_noop_default(self) -> None:
        """archive_expiring_indices 默认 no-op，返回 0。"""
        result = await archive_expiring_indices(before_days=30)
        assert result == 0


class TestArchiveExpiringIndices:
    @pytest.mark.asyncio
    async def test_returns_zero_by_default(self) -> None:
        assert await archive_expiring_indices(before_days=0) == 0
