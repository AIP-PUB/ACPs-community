"""tests/unit/test_access_runtime.py — D-4 AccessRuntime 生命周期测试。

TDD D-4：先写测试（红）→ 实现 AccessRuntime（绿）。
全部 Mock 外部依赖；不做真实 IO。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


class TestAccessRuntimeStart:
    @pytest.mark.asyncio
    async def test_testing_env_skips_tasks(self) -> None:
        from app.access.runtime import AccessRuntime

        rt = AccessRuntime()
        with (
            patch("app.access.runtime.get_settings") as mock_gs,
            patch("app.access.runtime.validate_access_config"),
            patch("app.access.runtime.store") as mock_store,
        ):
            mock_gs.return_value.app_env = "testing"
            mock_store.ensure_access_schema = AsyncMock()
            await rt.start()
        # No tasks registered in testing env
        assert rt._tasks == []

    @pytest.mark.asyncio
    async def test_config_error_propagates(self) -> None:
        from app.access.exception import AccessConfigError
        from app.access.runtime import AccessRuntime

        rt = AccessRuntime()
        with patch("app.access.runtime.validate_access_config") as mock_v:
            mock_v.side_effect = AccessConfigError(["bad config"])
            with pytest.raises(AccessConfigError):
                await rt.start()

    @pytest.mark.asyncio
    async def test_ensures_schema_on_start(self) -> None:
        from app.access.runtime import AccessRuntime

        rt = AccessRuntime()
        with (
            patch("app.access.runtime.validate_access_config"),
            patch("app.access.runtime.store") as mock_store,
            patch("app.access.runtime.get_settings") as mock_gs,
        ):
            mock_gs.return_value.app_env = "testing"
            mock_store.ensure_access_schema = AsyncMock()
            await rt.start()

        mock_store.ensure_access_schema.assert_awaited_once()


class TestAccessRuntimeStop:
    @pytest.mark.asyncio
    async def test_stop_is_idempotent_when_no_tasks(self) -> None:
        from app.access.runtime import AccessRuntime

        rt = AccessRuntime()
        with patch("app.access.runtime.close_clickhouse_client", AsyncMock()):
            await rt.stop()  # Should not raise

    @pytest.mark.asyncio
    async def test_stop_cancels_tasks(self) -> None:
        import asyncio

        from app.access.runtime import AccessRuntime

        rt = AccessRuntime()

        # Create a real task from a long-sleeping coroutine so it's cancellable
        async def _hang() -> None:
            await asyncio.sleep(1000)

        task = asyncio.create_task(_hang())
        rt._tasks = [task]

        with patch("app.access.runtime.close_clickhouse_client", AsyncMock()):
            await rt.stop()

        # After stop(), tasks list is cleared and the task was cancelled
        assert rt._tasks == []
        assert task.cancelled()


class TestValidateAccessConfig:
    """validate_access_config() 单元测试（C-2）。"""

    def test_valid_config_passes(self) -> None:
        """默认配置（testing.toml）不应抛异常。"""
        from app.access.runtime import validate_access_config

        validate_access_config()

    def test_invalid_lagging_mode_raises(self) -> None:
        """access_lagging_response_mode 非法值 → AccessConfigError。"""
        from app.access.exception import AccessConfigError
        from app.access.runtime import validate_access_config

        with patch("app.access.runtime.settings") as mock_s:
            _make_valid_settings(mock_s)
            mock_s.access_lagging_response_mode = "invalid"
            with pytest.raises(AccessConfigError) as exc_info:
                validate_access_config()
        assert exc_info.value.errors  # 至少有一条错误

    def test_error_status_threshold_mismatch_emits_warning(self) -> None:
        """access_error_status_threshold 与 MV 固化阈值不同时，应发出 warning 日志（C-2）。"""
        import structlog.testing

        from app.access.runtime import validate_access_config
        from app.access.tables import TOPOLOGY_MV_DEFAULT_ERROR_THRESHOLD

        different_threshold = TOPOLOGY_MV_DEFAULT_ERROR_THRESHOLD + 100

        with patch("app.access.runtime.settings") as mock_s:
            _make_valid_settings(mock_s)
            mock_s.access_error_status_threshold = different_threshold

            with structlog.testing.capture_logs() as cap_logs:
                validate_access_config()

        warning_found = any(
            record.get("log_level") == "warning" and "error_status_threshold" in str(record.get("event", ""))
            for record in cap_logs
        )
        assert warning_found, f"期望发出 error_status_threshold 不一致 warning，但未找到。Captured logs: {cap_logs}"

    def test_no_warning_when_threshold_matches(self) -> None:
        """access_error_status_threshold == 固化阈值时，不发 warning（无噪声）。"""
        import structlog.testing

        from app.access.runtime import validate_access_config
        from app.access.tables import TOPOLOGY_MV_DEFAULT_ERROR_THRESHOLD

        with patch("app.access.runtime.settings") as mock_s:
            _make_valid_settings(mock_s)
            mock_s.access_error_status_threshold = TOPOLOGY_MV_DEFAULT_ERROR_THRESHOLD

            with structlog.testing.capture_logs() as cap_logs:
                validate_access_config()

        threshold_warnings = [
            r
            for r in cap_logs
            if r.get("log_level") == "warning" and "error_status_threshold" in str(r.get("event", ""))
        ]
        assert not threshold_warnings, f"不应发出 threshold warning，但收到：{threshold_warnings}"

    def test_multiple_errors_collected(self) -> None:
        """多个非法配置一次性收集 → AccessConfigError.errors 包含所有错误。"""
        from app.access.exception import AccessConfigError
        from app.access.runtime import validate_access_config

        with patch("app.access.runtime.settings") as mock_s:
            _make_valid_settings(mock_s)
            mock_s.access_insert_batch_interval_seconds = -1
            mock_s.access_insert_batch_max_rows = -1
            mock_s.access_lagging_response_mode = "bad"

            with pytest.raises(AccessConfigError) as exc_info:
                validate_access_config()

        assert len(exc_info.value.errors) >= 3


def _make_valid_settings(mock_s: Any) -> None:
    """配置 mock_s 为有效的最小配置（避免测试互相污染）。"""
    from app.access.tables import TOPOLOGY_MV_DEFAULT_ERROR_THRESHOLD

    mock_s.access_insert_batch_interval_seconds = 5
    mock_s.access_insert_batch_max_rows = 1000
    mock_s.access_raw_retention_days = 7
    mock_s.access_archive_retention_days = 30
    mock_s.access_topology_retention_days = 30
    mock_s.access_lagging_threshold_ms = 300_000
    mock_s.access_query_timeout_seconds = 10
    mock_s.access_trace_max_spans = 500
    mock_s.access_slow_top_max_n = 20
    mock_s.access_error_attribution_max_n = 50
    mock_s.access_dedup_window_hours = 24
    mock_s.access_trace_max_duration_hours = 24
    mock_s.access_lagging_response_mode = "partial"
    mock_s.access_error_status_threshold = TOPOLOGY_MV_DEFAULT_ERROR_THRESHOLD
