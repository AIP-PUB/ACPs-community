"""tests/unit/test_access_archive.py — AccessArchiveTask 单元测试（E-3）。

TDD E-3：测试覆盖归档窗口计算、幂等防重复导出、Redis 标记读写、导出失败的异常处理。
外部依赖（Redis / ClickHouse）全部 mock，纯逻辑校验。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── helpers ──────────────────────────────────────────────────────────────────


def _make_redis_mock(get_return: Any = None) -> Any:
    """生成模拟 Redis 异步客户端。"""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=get_return)
    redis.set = AsyncMock(return_value=True)
    return redis


def _make_task(redis: Any = None) -> Any:
    """创建 AccessArchiveTask 实例（注入 mock redis）。"""
    from app.access.archive import AccessArchiveTask

    if redis is None:
        redis = _make_redis_mock()
    return AccessArchiveTask(redis)


# ── 测试 _find_dates_to_archive ───────────────────────────────────────────────


class TestFindDatesToArchive:
    """归档日期段计算逻辑。"""

    def test_returns_list_within_window(self) -> None:
        """应返回从 (today - archive_retention_days) 到 (today - raw_retention_days + 3) 的日期列表。"""
        task = _make_task()
        with (
            patch("app.access.archive.settings") as mock_s,
            patch("app.access.archive.datetime") as mock_dt,
        ):
            mock_s.access_raw_retention_days = 30
            mock_s.access_archive_retention_days = 35
            # 假设今天是 2026-02-10
            mock_dt.now.return_value = datetime(2026, 2, 10, tzinfo=UTC)
            dates = task._find_dates_to_archive()

        # 窗口: (2026-02-10 - 35d) = 2026-01-06 → (2026-02-10 - 30d + 3d) = 2026-01-14
        assert isinstance(dates, list)
        assert all(isinstance(d, date) for d in dates)
        # 9 天（2026-01-06 到 2026-01-14 包含）
        assert len(dates) == 9
        assert dates[0] == date(2026, 1, 6)
        assert dates[-1] == date(2026, 1, 14)

    def test_empty_when_window_inverted(self) -> None:
        """archive_retention_days 很小时（< raw_retention_days），窗口为空。"""
        task = _make_task()
        with (
            patch("app.access.archive.settings") as mock_s,
            patch("app.access.archive.datetime") as mock_dt,
        ):
            # raw=90，archive=30 → start_date > end_date → 空
            mock_s.access_raw_retention_days = 90
            mock_s.access_archive_retention_days = 30
            mock_dt.now.return_value = datetime(2026, 2, 10, tzinfo=UTC)
            dates = task._find_dates_to_archive()

        assert dates == []

    def test_dates_are_sorted_ascending(self) -> None:
        """日期列表应按升序排列（最早分区先归档）。"""
        task = _make_task()
        with (
            patch("app.access.archive.settings") as mock_s,
            patch("app.access.archive.datetime") as mock_dt,
        ):
            mock_s.access_raw_retention_days = 30
            mock_s.access_archive_retention_days = 40
            mock_dt.now.return_value = datetime(2026, 2, 10, tzinfo=UTC)
            dates = task._find_dates_to_archive()

        for i in range(len(dates) - 1):
            assert dates[i] < dates[i + 1]


# ── 测试 _is_already_exported ─────────────────────────────────────────────────


class TestIsAlreadyExported:
    """幂等检查：Redis 标记读取。"""

    @pytest.mark.asyncio
    async def test_returns_true_when_redis_key_exists(self) -> None:
        redis = _make_redis_mock(get_return=b"2026-01-06T00:00:00+00:00")
        task = _make_task(redis=redis)
        result = await task._is_already_exported("20260106")
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_redis_key_missing(self) -> None:
        redis = _make_redis_mock(get_return=None)
        task = _make_task(redis=redis)
        result = await task._is_already_exported("20260106")
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_redis_error(self) -> None:
        """Redis 不可用时保守返回 False（不阻断导出流程）。"""
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=ConnectionError("Redis unavailable"))
        task = _make_task(redis=redis)
        result = await task._is_already_exported("20260106")
        assert result is False


# ── 测试 _mark_exported ───────────────────────────────────────────────────────


class TestMarkExported:
    @pytest.mark.asyncio
    async def test_calls_redis_set_with_ttl(self) -> None:
        redis = _make_redis_mock()
        task = _make_task(redis=redis)
        await task._mark_exported("20260106")
        redis.set.assert_awaited_once()
        call_args = redis.set.call_args
        assert "20260106" in call_args.args[0]  # key 包含日期
        assert call_args.kwargs.get("ex") is not None  # 有 TTL

    @pytest.mark.asyncio
    async def test_silently_ignores_redis_error(self) -> None:
        """Redis 写标记失败不应抛出异常（不影响已完成的 Parquet 导出）。"""
        redis = AsyncMock()
        redis.set = AsyncMock(side_effect=ConnectionError("Redis write failed"))
        task = _make_task(redis=redis)
        # 不应 raise
        await task._mark_exported("20260106")


# ── 测试 _export_date ─────────────────────────────────────────────────────────


class TestExportDate:
    @pytest.mark.asyncio
    async def test_calls_clickhouse_command(self) -> None:
        """_export_date 应调用 ClickHouse client.command 并传入正确 S3 SQL。"""
        task = _make_task()
        mock_client = AsyncMock()
        mock_client.command = AsyncMock()

        with (
            patch("app.core.clickhouse_client.get_clickhouse_client", AsyncMock(return_value=mock_client)),
            patch("app.access.archive.settings") as mock_s,
        ):
            mock_s.access_minio_endpoint = "http://dev-minio:9000"
            mock_s.access_minio_bucket = "amp-access-archive"
            mock_s.minio_access_key = "admin"
            mock_s.minio_secret_key = "devpass"
            mock_s.access_minio_secure = False
            mock_s.clickhouse_database = "amp"

            target = date(2026, 1, 6)
            await task._export_date(target, "20260106")

        mock_client.command.assert_awaited_once()
        sql_arg = mock_client.command.call_args.args[0]
        assert "s3(" in sql_arg
        assert "amp-access-archive" in sql_arg
        assert "20260106" in sql_arg
        assert "Parquet" in sql_arg

    @pytest.mark.asyncio
    async def test_raises_on_clickhouse_error(self) -> None:
        """CH 命令失败时应包装为 ClickHouseInsertError 并抛出。"""
        from app.access.exception import ClickHouseInsertError

        task = _make_task()
        mock_client = AsyncMock()
        mock_client.command = AsyncMock(side_effect=RuntimeError("CH error"))

        with (
            patch("app.core.clickhouse_client.get_clickhouse_client", AsyncMock(return_value=mock_client)),
            patch("app.access.archive.settings") as mock_s,
        ):
            mock_s.access_minio_endpoint = "http://dev-minio:9000"
            mock_s.access_minio_bucket = "amp-access-archive"
            mock_s.minio_access_key = "admin"
            mock_s.minio_secret_key = "devpass"
            mock_s.access_minio_secure = False
            mock_s.clickhouse_database = "amp"

            with pytest.raises(ClickHouseInsertError):
                await task._export_date(date(2026, 1, 6), "20260106")


# ── 测试 run_once ─────────────────────────────────────────────────────────────


class TestRunOnce:
    @pytest.mark.asyncio
    async def test_skips_already_exported_dates(self) -> None:
        """已导出的日期应跳过（不调用 _export_date）。"""
        redis = _make_redis_mock(get_return=b"exported")  # 所有 get 返回非 None
        task = _make_task(redis=redis)

        with (
            patch.object(task, "_find_dates_to_archive", return_value=[date(2026, 1, 6)]),
            patch.object(task, "_export_date", AsyncMock()) as mock_export,
            patch.object(task, "_mark_exported", AsyncMock()),
        ):
            await task.run_once()

        mock_export.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exports_new_dates_and_marks_them(self) -> None:
        """新日期应调用 _export_date 并标记为已导出。"""
        redis = _make_redis_mock(get_return=None)  # 全部 get 返回 None（未导出）
        task = _make_task(redis=redis)

        export_calls: list[Any] = []

        async def fake_export(d: date, s: str) -> None:
            export_calls.append(s)

        with (
            patch.object(task, "_find_dates_to_archive", return_value=[date(2026, 1, 6), date(2026, 1, 7)]),
            patch.object(task, "_export_date", side_effect=fake_export),
            patch.object(task, "_mark_exported", AsyncMock()) as mock_mark,
        ):
            await task.run_once()

        assert export_calls == ["20260106", "20260107"]
        assert mock_mark.await_count == 2

    @pytest.mark.asyncio
    async def test_continues_on_export_failure(self) -> None:
        """单个日期导出失败不应中止后续日期的处理。"""
        redis = _make_redis_mock(get_return=None)
        task = _make_task(redis=redis)

        call_count = [0]

        async def fake_export(d: date, s: str) -> None:
            call_count[0] += 1
            if s == "20260106":
                raise RuntimeError("export failed")

        with (
            patch.object(task, "_find_dates_to_archive", return_value=[date(2026, 1, 6), date(2026, 1, 7)]),
            patch.object(task, "_export_date", side_effect=fake_export),
            patch.object(task, "_mark_exported", AsyncMock()) as mock_mark,
        ):
            await task.run_once()

        # 2 个日期都尝试了 export
        assert call_count[0] == 2
        # 只有成功的 20260107 被标记
        assert mock_mark.await_count == 1

    @pytest.mark.asyncio
    async def test_empty_dates_no_export(self) -> None:
        """无待归档日期时不触发任何 export。"""
        task = _make_task()
        with (
            patch.object(task, "_find_dates_to_archive", return_value=[]),
            patch.object(task, "_export_date", AsyncMock()) as mock_export,
        ):
            await task.run_once()

        mock_export.assert_not_awaited()


# ── 测试 run / stop ───────────────────────────────────────────────────────────


class TestRunAndStop:
    @pytest.mark.asyncio
    async def test_stop_sets_running_false(self) -> None:
        task = _make_task()
        task.stop()
        assert task._running is False

    @pytest.mark.asyncio
    async def test_run_exits_on_cancelled_error(self) -> None:
        """run() 在 CancelledError 时正常退出（无 exception 泄漏）。"""
        task = _make_task()

        call_count = [0]

        async def fake_run_once() -> None:
            call_count[0] += 1
            if call_count[0] >= 1:
                raise asyncio.CancelledError()

        with (
            patch.object(task, "run_once", side_effect=fake_run_once),
            patch("app.access.archive.settings") as mock_s,
        ):
            mock_s.access_archive_interval_seconds = 0

            # run() 应在 CancelledError 后正常 return（不 raise）
            await task.run()

        assert call_count[0] >= 1

    @pytest.mark.asyncio
    async def test_run_continues_after_exception(self) -> None:
        """run_once 抛非 Cancelled 异常时 run() 应继续循环（不 crash）。"""
        task = _make_task()

        call_count = [0]

        async def fake_run_once() -> None:
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("transient error")
            # 第二次调用后设置 running=False 终止循环
            task.stop()

        async def fake_sleep(n: float) -> None:
            pass

        with (
            patch.object(task, "run_once", side_effect=fake_run_once),
            patch("app.access.archive.asyncio.sleep", fake_sleep),
            patch("app.access.archive.settings") as mock_s,
        ):
            mock_s.access_archive_interval_seconds = 0
            await task.run()

        assert call_count[0] == 2


# ── 测试 AccessRuntime 集成（archive task 生命周期）────────────────────────────


class TestAccessRuntimeArchive:
    @pytest.mark.asyncio
    async def test_archive_task_not_started_when_disabled(self) -> None:
        """access_archive_enabled=False 时不创建归档任务。"""
        from app.access.runtime import AccessRuntime

        runtime = AccessRuntime()
        mock_writer = AsyncMock()
        # mock_writer.run is already an AsyncMock; let it be so coroutine lifecycle is properly managed

        with (
            patch("app.access.runtime.validate_access_config"),
            patch("app.access.runtime.store.ensure_access_schema", AsyncMock()),
            patch("app.access.runtime.get_settings") as mock_get_settings,
            patch("app.access.writer.AccessWriter", return_value=mock_writer),
            patch("app.core.redis_client.get_redis", return_value=AsyncMock()),
            patch("app.access.metrics.metrics_log_loop", new_callable=AsyncMock),
        ):
            mock_settings = MagicMock()
            mock_settings.app_env = "production"
            mock_settings.access_archive_enabled = False
            mock_settings.access_metrics_log_interval_seconds = 60
            mock_get_settings.return_value = mock_settings

            await runtime.start()

        # 无 archive task 时 _archive 应为 None
        assert runtime._archive is None
        for task in runtime._tasks:
            task.cancel()
        if runtime._tasks:
            await asyncio.gather(*runtime._tasks, return_exceptions=True)
