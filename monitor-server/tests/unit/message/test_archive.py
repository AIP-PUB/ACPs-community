"""单元测试：G-1 archive.py — 归档辅助纯函数与任务接口（设计 §6.21）。"""

from __future__ import annotations


class TestRawArchiveIsTrustworthy:
    def test_event_in_hot_window_is_trustworthy(self) -> None:
        from app.message.archive import raw_archive_is_trustworthy

        now_ms = 1_000_000_000
        raw_retention_ms = 7 * 86_400_000  # 7 天
        # min event ts is well within hot window
        min_event_ts_ms = now_ms - 24 * 3600_000  # 1 天前
        assert (
            raw_archive_is_trustworthy(
                min_event_ts_ms=min_event_ts_ms,
                raw_retention_ms=raw_retention_ms,
                now_ms=now_ms,
            )
            is True
        )

    def test_event_beyond_retention_is_not_trustworthy(self) -> None:
        from app.message.archive import raw_archive_is_trustworthy

        now_ms = 1_000_000_000
        raw_retention_ms = 7 * 86_400_000
        # min event ts is older than retention window
        min_event_ts_ms = now_ms - 8 * 86_400_000  # 8 天前
        assert (
            raw_archive_is_trustworthy(
                min_event_ts_ms=min_event_ts_ms,
                raw_retention_ms=raw_retention_ms,
                now_ms=now_ms,
            )
            is False
        )

    def test_exactly_at_boundary_is_trustworthy(self) -> None:
        from app.message.archive import raw_archive_is_trustworthy

        now_ms = 1_000_000_000
        raw_retention_ms = 7 * 86_400_000
        # exactly at the retention boundary
        min_event_ts_ms = now_ms - raw_retention_ms
        assert (
            raw_archive_is_trustworthy(
                min_event_ts_ms=min_event_ts_ms,
                raw_retention_ms=raw_retention_ms,
                now_ms=now_ms,
            )
            is True
        )

    def test_pure_function_no_side_effects(self) -> None:
        from app.message.archive import raw_archive_is_trustworthy

        now_ms = 1_000_000_000
        raw_retention_ms = 86_400_000
        result1 = raw_archive_is_trustworthy(
            min_event_ts_ms=now_ms - 1000,
            raw_retention_ms=raw_retention_ms,
            now_ms=now_ms,
        )
        result2 = raw_archive_is_trustworthy(
            min_event_ts_ms=now_ms - 1000,
            raw_retention_ms=raw_retention_ms,
            now_ms=now_ms,
        )
        assert result1 == result2


class TestMessageArchiveTask:
    def test_import(self) -> None:
        from app.message.archive import MessageArchiveTask

        assert MessageArchiveTask is not None

    def test_has_required_methods(self) -> None:
        import inspect

        from app.message.archive import MessageArchiveTask

        assert callable(getattr(MessageArchiveTask, "run", None))
        assert callable(getattr(MessageArchiveTask, "run_once", None))
        assert callable(getattr(MessageArchiveTask, "stop", None))
        assert inspect.iscoroutinefunction(MessageArchiveTask.run)
        assert inspect.iscoroutinefunction(MessageArchiveTask.run_once)

    def test_run_once_returns_zero_noop(self) -> None:
        import asyncio
        from unittest.mock import MagicMock

        from app.message.archive import MessageArchiveTask

        redis = MagicMock()
        task = MessageArchiveTask(redis)
        result = asyncio.new_event_loop().run_until_complete(task.run_once())
        assert result == 0

    def test_stop_is_idempotent(self) -> None:
        from unittest.mock import MagicMock

        from app.message.archive import MessageArchiveTask

        redis = MagicMock()
        task = MessageArchiveTask(redis)
        task.stop()
        task.stop()  # second call should not raise
