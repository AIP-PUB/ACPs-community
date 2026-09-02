"""tests/unit/system/test_runtime.py — runtime.py 配置校验 + bootstrap 单元测试。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.system.exception import SystemConfigError
from app.system.runtime import SystemRuntime, validate_system_config


def _make_valid_settings(**overrides: Any) -> MagicMock:
    defaults = {
        "system_bulk_index_batch_interval_seconds": 5,
        "system_bulk_index_batch_max_docs": 5000,
        "system_event_hot_retention_days": 3,
        "system_event_warm_retention_days": 14,
        "system_archive_retention_days": 30,
        "system_lagging_threshold_ms": 300000,
        "system_query_timeout_seconds": 30,
        "system_keyword_min_length": 3,
        "system_search_text_max_length": 8192,
        "system_freshness_reorder_margin_ms": 30000,
        "system_keyword_only_max_window_seconds": 3600,
        "system_lagging_response_mode": "partial",
        "system_index_number_of_shards": 3,
        "system_index_number_of_replicas": 1,
        "system_pit_keep_alive": "5m",
        "system_writer_enabled": True,
        "system_archive_enabled": False,
        "system_metrics_log_interval_seconds": 60,
        "app_env": "testing",
    }
    defaults.update(overrides)
    m = MagicMock()
    for k, v in defaults.items():
        setattr(m, k, v)
    return m


class TestValidateSystemConfig:
    def test_valid_config_passes(self) -> None:
        with patch("app.core.config.settings", _make_valid_settings()):
            validate_system_config()  # should not raise

    def test_warm_less_than_hot_fails(self) -> None:
        """system_event_warm_retention_days < hot → SystemConfigError。"""
        with patch(
            "app.core.config.settings",
            _make_valid_settings(
                system_event_hot_retention_days=10,
                system_event_warm_retention_days=5,  # < hot
            ),
        ):
            with pytest.raises(SystemConfigError) as exc_info:
                validate_system_config()
        assert "warm" in str(exc_info.value).lower()

    def test_archive_less_than_warm_fails(self) -> None:
        """system_archive_retention_days < warm → SystemConfigError。"""
        with patch(
            "app.core.config.settings",
            _make_valid_settings(
                system_event_warm_retention_days=20,
                system_archive_retention_days=10,  # < warm
            ),
        ):
            with pytest.raises(SystemConfigError):
                validate_system_config()

    def test_keyword_min_length_zero_fails(self) -> None:
        with patch("app.core.config.settings", _make_valid_settings(system_keyword_min_length=0)):
            with pytest.raises(SystemConfigError):
                validate_system_config()

    def test_invalid_lagging_response_mode_fails(self) -> None:
        with patch("app.core.config.settings", _make_valid_settings(system_lagging_response_mode="warn")):
            with pytest.raises(SystemConfigError):
                validate_system_config()

    def test_batch_interval_zero_fails(self) -> None:
        with patch("app.core.config.settings", _make_valid_settings(system_bulk_index_batch_interval_seconds=0)):
            with pytest.raises(SystemConfigError):
                validate_system_config()

    def test_search_text_max_length_zero_fails(self) -> None:
        with patch("app.core.config.settings", _make_valid_settings(system_search_text_max_length=0)):
            with pytest.raises(SystemConfigError):
                validate_system_config()

    def test_shards_zero_fails(self) -> None:
        """system_index_number_of_shards <= 0 → SystemConfigError。"""
        with patch("app.core.config.settings", _make_valid_settings(system_index_number_of_shards=0)):
            with pytest.raises(SystemConfigError) as exc_info:
                validate_system_config()
        assert "shards" in str(exc_info.value).lower()

    def test_replicas_negative_fails(self) -> None:
        """system_index_number_of_replicas < 0 → SystemConfigError。"""
        with patch("app.core.config.settings", _make_valid_settings(system_index_number_of_replicas=-1)):
            with pytest.raises(SystemConfigError) as exc_info:
                validate_system_config()
        assert "replicas" in str(exc_info.value).lower()

    def test_replicas_zero_passes(self) -> None:
        """system_index_number_of_replicas = 0 合法（单节点测试集群，§7.4）。"""
        with patch("app.core.config.settings", _make_valid_settings(system_index_number_of_replicas=0)):
            validate_system_config()  # should not raise

    def test_pit_keep_alive_empty_fails(self) -> None:
        """system_pit_keep_alive 为空 → SystemConfigError。"""
        with patch("app.core.config.settings", _make_valid_settings(system_pit_keep_alive="")):
            with pytest.raises(SystemConfigError) as exc_info:
                validate_system_config()
        assert "pit" in str(exc_info.value).lower()


class TestSystemRuntime:
    @pytest.mark.asyncio
    async def test_testing_env_skips_background_tasks(self) -> None:
        """app_env=testing → 跳过后台 IO 任务（不启动 writer/metrics_log_loop）。"""
        runtime = SystemRuntime()

        mock_ensure_schema = AsyncMock()
        with (
            patch("app.core.config.settings", _make_valid_settings(app_env="testing")),
            patch("app.system.store.ensure_system_schema", mock_ensure_schema),
        ):
            await runtime.start()

        assert len(runtime._tasks) == 0

    @pytest.mark.asyncio
    async def test_writer_disabled_does_not_start_writer(self) -> None:
        """system_writer_enabled=False → 不起 writer 任务。"""
        runtime = SystemRuntime()

        mock_ensure_schema = AsyncMock()
        with (
            patch("app.core.config.settings", _make_valid_settings(app_env="testing", system_writer_enabled=False)),
            patch("app.system.store.ensure_system_schema", mock_ensure_schema),
        ):
            await runtime.start()

        writer_tasks = [t for t in runtime._tasks if "writer" in getattr(t, "get_name", lambda: "")()]
        assert len(writer_tasks) == 0

    @pytest.mark.asyncio
    async def test_config_error_propagates(self) -> None:
        """配置非法 → SystemConfigError 上浮（fail-fast）。"""
        runtime = SystemRuntime()

        with patch("app.core.config.settings", _make_valid_settings(system_keyword_min_length=0)):
            with pytest.raises(SystemConfigError):
                await runtime.start()
