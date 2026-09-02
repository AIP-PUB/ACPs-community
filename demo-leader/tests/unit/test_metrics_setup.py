"""tests/unit/test_metrics_setup.py — metrics_setup.py 单元测试（Step E2）。"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

# ── start_metrics 幂等 ────────────────────────────────────────────────────────


def test_start_metrics_idempotent() -> None:
    """多次调用 start_metrics 不创建多个 task。"""
    with patch("assistant.metrics_setup.asyncio.create_task") as mock_create:
        mock_task = MagicMock()
        mock_task.done.return_value = False
        mock_create.return_value = mock_task

        from assistant.metrics_setup import start_metrics

        # 首次调用创建 task
        start_metrics()
        assert mock_create.call_count == 1

        # 第二次调用：task 未完成 → 不再创建
        start_metrics()
        assert mock_create.call_count == 1  # 仍为 1
        created_coro = mock_create.call_args.args[0]
        created_coro.close()


def test_start_metrics_recreates_task_when_done() -> None:
    """task 已完成时，start_metrics 可以重新创建 task。"""
    with patch("assistant.metrics_setup.asyncio.create_task") as mock_create:
        mock_done = MagicMock()
        mock_done.done.return_value = True  # 已完成

        mock_new = MagicMock()
        mock_new.done.return_value = False

        mock_create.return_value = mock_new

        import assistant.metrics_setup as ms

        ms._metrics_task = mock_done

        ms.start_metrics()
        assert mock_create.call_count == 1  # 重新创建
        created_coro = mock_create.call_args.args[0]
        created_coro.close()


# ── stop_metrics 正常取消 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_metrics_cancels_and_awaits() -> None:
    """stop_metrics 取消 task 且不抛 CancelledError。"""
    import assistant.metrics_setup as ms

    task = asyncio.create_task(asyncio.sleep(100))
    ms._metrics_task = task
    await ms.stop_metrics()
    assert ms._metrics_task is None
    assert task.cancelled()


def test_stop_metrics_noop_when_no_task() -> None:
    """_metrics_task 为 None 时 stop_metrics 不报错。"""
    import assistant.metrics_setup as ms

    ms._metrics_task = None

    async def run() -> None:
        await ms.stop_metrics()  # 不抛

    asyncio.run(run())


# ── _sample 回退（DemoMetricsSampler）────────────────────────────────────────


def test_sampler_sample_does_not_raise_with_none_active_tasks() -> None:
    """DemoMetricsSampler.sample() 在无真实任务计数时不 raise。"""
    from assistant.metrics_setup import _SAMPLER

    body = _SAMPLER.sample()
    assert body.uptime_seconds is not None
    assert body.load_metrics is not None
