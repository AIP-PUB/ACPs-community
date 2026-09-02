"""
D0 测试：GenericRunner 多监听者支持
"""

from __future__ import annotations

import asyncio
from datetime import UTC
from unittest.mock import MagicMock, patch

import pytest
from acps_sdk.aip.aip_base_model import TaskResult, TaskState, TaskStatus

# ---------------------------------------------------------------------------
# 简化版 GenericRunner stub（仅测试监听者逻辑所需的最小接口）
# 真正的 GenericRunner 需要加载文件系统资源，因此我们通过
# 直接调用 _update_task_status 触发回调来验证行为。
# ---------------------------------------------------------------------------


def _make_task_result(task_id: str = "t-1", state: TaskState = TaskState.Working) -> TaskResult:
    from datetime import datetime

    now = datetime.now(UTC).isoformat()
    return TaskResult(
        id=f"result-{task_id}",
        sentAt=now,
        senderRole="partner",
        senderId="agent",
        taskId=task_id,
        status=TaskStatus(state=state, stateChangedAt=now),
    )


@pytest.fixture()
def runner(tmp_path):
    """创建最小化 GenericRunner（mock 掉文件/LLM 依赖）。"""
    # 为 runner 创建必要的目录和配置文件
    import json

    agent_dir = tmp_path / "test_agent"
    agent_dir.mkdir()

    # acs.json
    (agent_dir / "acs.json").write_text(json.dumps({"aic": "test-aic", "capabilities": {}}), encoding="utf-8")
    # config.toml
    (agent_dir / "config.toml").write_text("[concurrency]\nmax_concurrent_tasks = 10\n")
    # prompts.toml
    (agent_dir / "prompts.toml").write_text("")
    # skills.toml（可选）
    (agent_dir / "skills.toml").write_text("")

    with (
        patch("partners.generic_runner.AuditEmitter"),
        patch("partners.generic_runner.HeartbeatEmitter"),
        patch("partners.generic_runner.MetricsEmitter"),
        patch("partners.generic_runner.DemoMetricsSampler"),
        patch("partners.generic_runner.load_signer_from_keys_json", return_value=MagicMock()),
    ):
        from partners.generic_runner import GenericRunner

        return GenericRunner("test_agent", str(agent_dir))


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_listeners_invoked(runner):
    """注册 2 个 listener，触发状态变更后两个均被调用。"""
    called = []

    async def listener_a(task_result: TaskResult) -> None:
        called.append("a")

    async def listener_b(task_result: TaskResult) -> None:
        called.append("b")

    runner.add_state_change_listener(listener_a)
    runner.add_state_change_listener(listener_b)

    task_id = "t-multi"
    from acps_sdk.aip.aip_base_model import TaskState

    # 预置一个 task
    from partners.generic_runner import TaskContext

    runner.tasks[task_id] = TaskContext(task=_make_task_result(task_id=task_id, state=TaskState.Accepted))

    runner._update_task_status(task_id, TaskState.Working)

    # fire-and-forget tasks 需要一个短暂的 loop 迭代让它们执行
    await asyncio.sleep(0)

    assert "a" in called
    assert "b" in called


@pytest.mark.asyncio
async def test_set_state_change_callback_backward_compat(runner):
    """旧 API set_state_change_callback 仍触发回调，且不覆盖已注册的新 listener。"""
    called_new = []
    called_old = []

    async def new_listener(task_result: TaskResult) -> None:
        called_new.append(task_result.taskId)

    async def old_callback(task_result: TaskResult) -> None:
        called_old.append(task_result.taskId)

    runner.add_state_change_listener(new_listener)
    runner.set_state_change_callback(old_callback)

    task_id = "t-compat"
    from partners.generic_runner import TaskContext

    runner.tasks[task_id] = TaskContext(task=_make_task_result(task_id=task_id, state=TaskState.Accepted))

    runner._update_task_status(task_id, TaskState.Working)
    await asyncio.sleep(0)

    assert task_id in called_new
    assert task_id in called_old


@pytest.mark.asyncio
async def test_listener_exception_isolated(runner):
    """一个 listener 抛异常，另一个仍被调用，任务状态流转不受影响。"""
    called = []

    async def bad_listener(task_result: TaskResult) -> None:
        raise RuntimeError("boom")

    async def good_listener(task_result: TaskResult) -> None:
        called.append(task_result.taskId)

    runner.add_state_change_listener(bad_listener)
    runner.add_state_change_listener(good_listener)

    task_id = "t-exception"
    from partners.generic_runner import TaskContext

    runner.tasks[task_id] = TaskContext(task=_make_task_result(task_id=task_id, state=TaskState.Accepted))

    runner._update_task_status(task_id, TaskState.Working)
    await asyncio.sleep(0.05)  # 给 bad_listener 的 task 执行并产生异常

    assert task_id in called
    # 任务状态应已变为 Working
    assert runner.tasks[task_id].task.status.state == TaskState.Working


@pytest.mark.asyncio
async def test_remove_state_change_listener(runner):
    """注册后 remove，状态变更时已移除的 listener 不被调用。"""
    called = []

    async def listener(task_result: TaskResult) -> None:
        called.append(task_result.taskId)

    runner.add_state_change_listener(listener)
    runner.remove_state_change_listener(listener)

    task_id = "t-remove"
    from partners.generic_runner import TaskContext

    runner.tasks[task_id] = TaskContext(task=_make_task_result(task_id=task_id, state=TaskState.Accepted))

    runner._update_task_status(task_id, TaskState.Working)
    await asyncio.sleep(0)

    assert called == []


@pytest.mark.asyncio
async def test_add_same_listener_no_dup(runner):
    """同一 listener 添加两次，仍只被调用一次。"""
    called = []

    async def listener(task_result: TaskResult) -> None:
        called.append(task_result.taskId)

    runner.add_state_change_listener(listener)
    runner.add_state_change_listener(listener)

    task_id = "t-nodup"
    from partners.generic_runner import TaskContext

    runner.tasks[task_id] = TaskContext(task=_make_task_result(task_id=task_id, state=TaskState.Accepted))

    runner._update_task_status(task_id, TaskState.Working)
    await asyncio.sleep(0)

    assert called.count(task_id) == 1
