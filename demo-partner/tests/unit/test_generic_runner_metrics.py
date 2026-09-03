"""tests/unit/test_generic_runner_metrics.py — GenericRunner 指标发射单元测试（Step E3）。"""

from __future__ import annotations

import asyncio
from datetime import UTC
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from acps_sdk.aip.aip_base_model import TaskState

if TYPE_CHECKING:
    from partners.generic_runner import GenericRunner, TaskContext

# ── 每个 agent 独立文件路径 ────────────────────────────────────────────────────


def test_different_agent_names_have_different_metrics_files(mock_generic_runner: GenericRunner) -> None:
    """不同 agent_name → amp_metrics_<agent_name>.jsonl 文件路径不同（E3-1）。"""
    from partners.generic_runner import GenericRunner

    base_dir = mock_generic_runner.base_dir
    with patch("partners.generic_runner.AsyncOpenAI"):
        runner_a = GenericRunner("agent_alpha", base_dir)
        runner_b = GenericRunner("agent_beta", base_dir)

    path_a = runner_a._metrics_emitter._log_file
    path_b = runner_b._metrics_emitter._log_file
    assert path_a != path_b
    assert "agent_alpha" in path_a.name
    assert "agent_beta" in path_b.name


def test_metrics_file_uses_agent_name(mock_generic_runner: GenericRunner) -> None:
    """metrics 文件路径包含 agent_name（E3-1）。"""
    emitter = mock_generic_runner._metrics_emitter
    assert mock_generic_runner.agent_name in emitter._log_file.name


# ── resource 标签包含 service.name ────────────────────────────────────────────


def test_metrics_resource_service_name_includes_agent_name(mock_generic_runner: GenericRunner) -> None:
    """resource["service.name"] = "demo-partner-{agent_name}"（E3-2）。"""
    emitter = mock_generic_runner._metrics_emitter
    assert emitter._resource is not None
    assert emitter._resource.get("service.name") == f"demo-partner-{mock_generic_runner.agent_name}"


# ── _sample_metrics 统计 ──────────────────────────────────────────────────────


def _make_task_ctx(state: TaskState) -> TaskContext:
    """构造最小化 TaskContext（不依赖真实 task_id）。"""
    import uuid
    from datetime import datetime

    from acps_sdk.aip.aip_base_model import TaskResult, TaskStatus

    from partners.generic_runner import TaskContext

    tid = str(uuid.uuid4())
    task = TaskResult(
        id=f"result-{tid}",
        sentAt=datetime.now(UTC).isoformat(),
        senderRole="partner",
        senderId="test",
        taskId=tid,
        sessionId="sess-001",
        status=TaskStatus(state=state, stateChangedAt=datetime.now(UTC).isoformat()),
    )
    return TaskContext(task=task)


def test_sample_metrics_counts_working_as_active(mock_generic_runner: GenericRunner) -> None:
    """Working 状态任务计入 active_tasks（E3-3）。"""
    mock_generic_runner.tasks = {
        "t1": _make_task_ctx(TaskState.Working),
        "t2": _make_task_ctx(TaskState.Working),
        "t3": _make_task_ctx(TaskState.Completed),
    }
    counts = mock_generic_runner._sample_metrics()
    assert counts["active_tasks"] == 2


def test_sample_metrics_counts_accepted_and_awaiting_as_queued(mock_generic_runner: GenericRunner) -> None:
    """Accepted + AwaitingCompletion 计入 queued_tasks（E3-3）。"""
    mock_generic_runner.tasks = {
        "t1": _make_task_ctx(TaskState.Accepted),
        "t2": _make_task_ctx(TaskState.AwaitingCompletion),
        "t3": _make_task_ctx(TaskState.Working),
        "t4": _make_task_ctx(TaskState.Completed),
    }
    counts = mock_generic_runner._sample_metrics()
    assert counts["queued_tasks"] == 2


def test_sample_metrics_empty_tasks(mock_generic_runner: GenericRunner) -> None:
    """无任务时 active = queued = 0。"""
    mock_generic_runner.tasks = {}
    counts = mock_generic_runner._sample_metrics()
    assert counts["active_tasks"] == 0
    assert counts["queued_tasks"] == 0


# ── start_metrics 幂等 + stop_metrics ────────────────────────────────────────


def test_start_metrics_idempotent(mock_generic_runner: GenericRunner) -> None:
    """多次调用 start_metrics 不创建多个 task。"""
    with patch("partners.generic_runner.asyncio.create_task") as mock_create:
        mock_task = mock_create.return_value
        mock_task.done.return_value = False

        mock_generic_runner.start_metrics()
        mock_generic_runner.start_metrics()
        assert mock_create.call_count == 1
        created_coro = mock_create.call_args.args[0]
        created_coro.close()


@pytest.mark.asyncio
async def test_stop_metrics_cancels_task(mock_generic_runner: GenericRunner) -> None:
    """stop_metrics 取消 task，不抛 CancelledError。"""
    task = asyncio.create_task(asyncio.sleep(100))
    mock_generic_runner._metrics_task = task
    await mock_generic_runner.stop_metrics()
    assert mock_generic_runner._metrics_task is None
    assert task.cancelled()
