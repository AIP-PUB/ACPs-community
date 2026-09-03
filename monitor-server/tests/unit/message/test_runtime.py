"""单元测试：G-2 runtime.py — 配置校验与 MessageRuntime 生命周期（设计 §6.23）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


class TestValidateMessageConfig:
    def test_valid_config_passes(self) -> None:
        from app.message.runtime import validate_message_config

        validate_message_config()  # testing.toml 为合法配置，不应抛异常

    def test_dedup_window_less_than_kafka_retention_fails(self) -> None:
        from unittest.mock import PropertyMock

        from app.message.exception import MessageConfigError
        from app.message.runtime import validate_message_config

        settings_cls = type(__import__("app.message.runtime", fromlist=["settings"]).settings)
        with (
            patch.object(settings_cls, "message_dedup_window_seconds", new_callable=PropertyMock, return_value=100),
            patch.object(settings_cls, "message_kafka_retention_seconds", new_callable=PropertyMock, return_value=200),
        ):
            try:
                validate_message_config()
            except MessageConfigError as exc:
                assert any("dedup" in e.lower() for e in exc.errors)
            else:
                raise AssertionError("Expected MessageConfigError")

    def test_lifecycle_retention_less_than_raw_retention_fails(self) -> None:
        from unittest.mock import PropertyMock

        from app.message.exception import MessageConfigError
        from app.message.runtime import validate_message_config

        settings_cls = type(__import__("app.message.runtime", fromlist=["settings"]).settings)
        with (
            patch.object(settings_cls, "message_lifecycle_retention_days", new_callable=PropertyMock, return_value=1),
            patch.object(settings_cls, "message_raw_retention_days", new_callable=PropertyMock, return_value=7),
        ):
            try:
                validate_message_config()
            except MessageConfigError as exc:
                assert any("lifecycle_retention" in e.lower() for e in exc.errors)
            else:
                raise AssertionError("Expected MessageConfigError")

    def test_collects_multiple_errors(self) -> None:
        from unittest.mock import PropertyMock

        from app.message.exception import MessageConfigError
        from app.message.runtime import validate_message_config

        settings_cls = type(__import__("app.message.runtime", fromlist=["settings"]).settings)
        with (
            patch.object(settings_cls, "message_dedup_window_seconds", new_callable=PropertyMock, return_value=1),
            patch.object(settings_cls, "message_kafka_retention_seconds", new_callable=PropertyMock, return_value=9999),
            patch.object(settings_cls, "message_lifecycle_retention_days", new_callable=PropertyMock, return_value=1),
            patch.object(settings_cls, "message_raw_retention_days", new_callable=PropertyMock, return_value=100),
        ):
            try:
                validate_message_config()
            except MessageConfigError as exc:
                assert len(exc.errors) >= 2
            else:
                raise AssertionError("Expected MessageConfigError")


class TestMessageRuntime:
    def test_import(self) -> None:
        from app.message.runtime import MessageRuntime

        assert MessageRuntime is not None

    def test_init_has_empty_tasks(self) -> None:
        from app.message.runtime import MessageRuntime

        rt = MessageRuntime()
        assert rt._tasks == []

    @pytest.mark.asyncio
    async def test_start_skips_background_tasks_in_testing(self) -> None:
        from app.message.runtime import MessageRuntime

        rt = MessageRuntime()
        with patch("app.message.runtime.store.ensure_message_schema", AsyncMock()):
            await rt.start()
        assert rt._tasks == []

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self) -> None:
        from app.message.runtime import MessageRuntime

        rt = MessageRuntime()
        await rt.stop()
        await rt.stop()

    @pytest.mark.asyncio
    async def test_start_calls_validate_config(self) -> None:
        from app.message.exception import MessageConfigError
        from app.message.runtime import MessageRuntime

        rt = MessageRuntime()
        with patch("app.message.runtime.validate_message_config", side_effect=MessageConfigError(["bad"])):
            try:
                await rt.start()
            except MessageConfigError:
                pass
            else:
                raise AssertionError("Expected MessageConfigError")

    @pytest.mark.asyncio
    async def test_start_calls_ensure_schema(self) -> None:
        from app.message.runtime import MessageRuntime

        rt = MessageRuntime()
        ensure_mock = AsyncMock()
        with patch("app.message.runtime.store.ensure_message_schema", ensure_mock):
            await rt.start()
        ensure_mock.assert_awaited_once()
